import pytest
from django.urls import reverse

from apps.accounts.models import User
from apps.core.models import PublishStatus
from apps.learning import services
from apps.learning.models import Chapter, Ebook, MediaAsset, MediaKind, Subchapter
from apps.learning.tests.conftest import sign_in, uploaded_image
from apps.staff.authz import PermissionDeniedError

pytestmark = pytest.mark.django_db

EBOOK_URL = reverse("learn-ebook")
CHANGELIST = reverse("admin:learning_ebook_changelist")


def ebook_url(slug: str) -> str:
    return reverse("learn-ebook-chapter", args=[slug])


@pytest.fixture
def live_ebook(make_ebook, publisher):
    ebook = make_ebook(description="Every rule, with room to write.")
    services.publish(actor=publisher, instance=ebook)
    ebook.refresh_from_db()
    return ebook


@pytest.fixture
def highway_code(publisher):
    """The book read out of the hierarchy, which the e-book endpoints have always served."""
    book = Chapter.objects.create(slug="highway-code", title="The Highway Code", status=PublishStatus.PUBLISHED)
    section = Subchapter.objects.create(chapter=book, slug="hierarchy", title="The hierarchy", order=1)
    services.publish(actor=publisher, instance=section)
    return section


class TestTheSlug:
    """An address, so it is filled in when staff leave it empty and never used twice."""

    def test_it_is_made_from_the_title_when_one_is_not_given(self, db):
        document = MediaAsset.objects.create(title="Workbook", kind=MediaKind.DOCUMENT)

        ebook = Ebook.objects.create(title="Road Signs Workbook", file=document)

        assert ebook.slug == "road-signs-workbook"

    def test_two_ebooks_with_the_same_title_get_different_slugs(self, db):
        document = MediaAsset.objects.create(title="Workbook", kind=MediaKind.DOCUMENT)

        first = Ebook.objects.create(title="Workbook", file=document)
        second = Ebook.objects.create(title="Workbook", file=document)

        assert (first.slug, second.slug) == ("workbook", "workbook-2")

    def test_the_same_slug_cannot_be_used_twice(self, make_ebook):
        from django.db import IntegrityError, transaction

        document = make_ebook(slug="workbook").file

        with pytest.raises(IntegrityError), transaction.atomic():
            Ebook.objects.create(slug="workbook", title="Another", file=document)


class TestPublishing:
    def test_it_cannot_be_published_with_a_video_instead_of_a_document(self, make_ebook, publisher):
        """The reader would be handed a video and asked to read it."""
        video = MediaAsset.objects.create(title="Clip", kind=MediaKind.VIDEO)

        with pytest.raises(services.NotReadyToPublishError) as refused:
            services.publish(actor=publisher, instance=make_ebook(file=video))

        assert "not a document" in str(refused.value)

    def test_a_publisher_can_release_one(self, make_ebook, publisher):
        ebook = services.publish(actor=publisher, instance=make_ebook())

        assert ebook.status == PublishStatus.PUBLISHED
        assert ebook.published_by == publisher

    def test_an_editor_cannot(self, make_ebook, editor):
        """Writing material and releasing it are two jobs, here as everywhere."""
        with pytest.raises(PermissionDeniedError):
            services.publish(actor=editor, instance=make_ebook())


class TestTheListing:
    def test_a_student_sees_the_published_ebooks(self, api, student, live_ebook):
        live_ebook.cover.save("cover.png", uploaded_image("cover.png"), save=True)

        book = sign_in(api, student()).get(EBOOK_URL).data["ebooks"][0]

        assert book["slug"] == "highway-code-workbook"
        assert book["title"] == "Highway Code workbook"
        assert book["description"] == "Every rule, with room to write."
        assert book["coverUrl"] == live_ebook.cover.url

    def test_the_shelf_does_not_hand_out_the_file(self, api, student, live_ebook):
        """Browsing is not downloading: the signed link comes from the detail endpoint."""
        book = sign_in(api, student()).get(EBOOK_URL).data["ebooks"][0]

        assert "url" not in book
        assert live_ebook.file.file.name not in str(book)

    def test_drafts_are_not_listed(self, api, student, make_ebook):
        make_ebook(slug="unreleased", title="Unreleased")

        assert sign_in(api, student()).get(EBOOK_URL).data["ebooks"] == []

    def test_the_highway_code_chapters_are_still_listed_beside_them(self, api, student, live_ebook, highway_code):
        """The reader has always been served the book out of the hierarchy. That is unchanged."""
        response = sign_in(api, student()).get(EBOOK_URL)

        assert [item["slug"] for item in response.data["chapters"]] == ["hierarchy"]
        assert [item["slug"] for item in response.data["ebooks"]] == ["highway-code-workbook"]
        assert response.data["current"] == "hierarchy"


class TestReadingOne:
    def test_a_student_gets_the_ebook_and_its_document(self, api, student, live_ebook):
        book = sign_in(api, student()).get(ebook_url("highway-code-workbook")).data["ebook"]

        assert book["slug"] == "highway-code-workbook"
        assert book["url"] == live_ebook.file.file.url
        assert book["contentType"] == "application/pdf"
        assert book["sizeBytes"] == live_ebook.file.size_bytes

    def test_an_unknown_slug_is_not_found(self, api, student, live_ebook):
        assert sign_in(api, student()).get(ebook_url("no-such-book")).status_code == 404

    def test_a_draft_is_not_found(self, api, student, make_ebook):
        """Not 403: a draft is not something to be told exists."""
        make_ebook(slug="unreleased", title="Unreleased")

        assert sign_in(api, student()).get(ebook_url("unreleased")).status_code == 404

    def test_somebody_without_a_plan_is_asked_to_buy_one(self, api, live_ebook, db):
        """The signed link to the file is what a plan buys."""
        browsing = User.objects.create_user(email="browsing@example.com", first_name="Bo")

        assert sign_in(api, browsing).get(ebook_url("highway-code-workbook")).status_code == 402

    def test_a_highway_code_section_is_still_read_at_the_same_route(self, api, student, highway_code):
        """The slug decides which of the two comes back, and neither shadows the other."""
        response = sign_in(api, student()).get(ebook_url("hierarchy"))

        assert response.data["chapter"]["slug"] == "hierarchy"
        assert "ebook" not in response.data


class TestTheAdminSection:
    def test_it_appears_in_the_menu_for_a_content_editor(self, editor_client):
        page = editor_client.get(reverse("admin:index")).content.decode()

        assert "Ebooks" in page

    def test_an_editor_can_create_one(self, editor_client, db):
        document = MediaAsset.objects.create(title="Workbook", kind=MediaKind.DOCUMENT)

        response = editor_client.post(
            reverse("admin:learning_ebook_add"),
            {
                "title": "Road signs workbook",
                "slug": "road-signs-workbook",
                "description": "Every sign, with room to write.",
                "file": document.pk,
                "order": 1,
                "publish_from": "",
            },
            follow=True,
        )

        assert response.status_code == 200
        assert Ebook.objects.get(slug="road-signs-workbook").title == "Road signs workbook"

    def test_a_slug_already_in_use_is_refused_with_a_message(self, editor_client, make_ebook):
        document = make_ebook(slug="workbook", title="Workbook").file

        response = editor_client.post(
            reverse("admin:learning_ebook_add"),
            {
                "title": "Another",
                "slug": "workbook",
                "description": "",
                "file": document.pk,
                "order": 1,
                "publish_from": "",
            },
        )

        assert "already exists" in response.content.decode()
        assert Ebook.objects.count() == 1

    def test_a_publisher_can_publish_from_the_list(self, publisher_client, make_ebook):
        ebook = make_ebook()

        publisher_client.post(CHANGELIST, {"action": "publish_selected", "_selected_action": [ebook.pk]}, follow=True)

        ebook.refresh_from_db()
        assert ebook.status == PublishStatus.PUBLISHED


class TestThePreview:
    """ "See it as a student" for an e-book, which is a file rather than a page of material.

    It used to fall through to the empty-page message and say "nothing is published in here yet",
    which read as if something were wrong with the book.
    """

    def preview_url(self, ebook) -> str:
        return reverse("admin:learning_ebook_preview", args=[ebook.pk])

    def test_it_offers_the_document_rather_than_claiming_the_ebook_is_empty(self, editor_client, make_ebook):
        ebook = make_ebook()

        page = editor_client.get(self.preview_url(ebook)).content.decode()

        assert "Nothing is published in here yet" not in page
        assert "Open the document" in page
        assert ebook.file.file.url in page

    def test_it_shows_the_cover(self, editor_client, make_ebook):
        ebook = make_ebook()
        ebook.cover.save("cover.png", uploaded_image("cover.png"), save=True)

        page = editor_client.get(self.preview_url(ebook)).content.decode()

        assert f'<img class="adm-preview__cover" src="{ebook.cover.url}"' in page

    def test_it_still_reports_what_would_stop_publishing(self, editor_client, make_ebook):
        """The warning is the point of the page; showing the file must not have replaced it."""
        video = MediaAsset.objects.create(title="Clip", kind=MediaKind.VIDEO)

        page = editor_client.get(self.preview_url(make_ebook(file=video))).content.decode()

        assert "This cannot be published yet" in page
        assert "not a document" in page
