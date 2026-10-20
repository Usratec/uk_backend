import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core.models import PublishStatus
from apps.learning import services
from apps.learning.models import Ebook, EbookSection, EbookSectionProgress, MediaAsset, MediaKind
from apps.learning.tests.conftest import sign_in, uploaded_image
from apps.staff.authz import PermissionDeniedError

pytestmark = pytest.mark.django_db

EBOOK_URL = reverse("learn-ebook")
CHANGELIST = reverse("admin:learning_ebook_changelist")


def ebook_url(slug: str) -> str:
    return reverse("learn-ebook-detail", args=[slug])


@pytest.fixture
def live_ebook(make_ebook, publisher):
    ebook = make_ebook(description="Every rule, with room to write.")
    services.publish(actor=publisher, instance=ebook)
    ebook.refresh_from_db()
    return ebook


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

    def test_a_book_with_no_document_can_still_be_published(self, make_ebook, publisher):
        book = services.publish(actor=publisher, instance=make_ebook(file=None))

        assert book.status == PublishStatus.PUBLISHED


class TestPublishingASection:
    def test_a_publisher_can_release_one(self, make_section, read_online, publisher):
        section = services.publish(
            actor=publisher, instance=make_section(read_online, slug="signals", title="Signals", order=2)
        )

        assert section.status == PublishStatus.PUBLISHED
        assert section.published_by == publisher

    def test_an_editor_cannot(self, make_section, read_online, editor):
        with pytest.raises(PermissionDeniedError):
            services.publish(actor=editor, instance=make_section(read_online, slug="signals", order=2))

    def test_an_empty_section_is_refused(self, make_section, read_online, publisher):
        with pytest.raises(services.NotReadyToPublishError) as refused:
            services.publish(
                actor=publisher, instance=make_section(read_online, slug="signals", order=2, body_html="")
            )

        assert "nothing in this section" in str(refused.value)

    def test_a_file_alone_is_enough(self, make_section, read_online, publisher):
        """A section can be a video or a document with nothing written around it."""
        video = MediaAsset.objects.create(title="Clip", kind=MediaKind.VIDEO)

        section = services.publish(
            actor=publisher,
            instance=make_section(read_online, slug="signals", order=2, body_html="", media=video),
        )

        assert section.status == PublishStatus.PUBLISHED

    def test_a_picture_is_refused_because_it_belongs_in_the_text(self, make_section, read_online, publisher):
        image = MediaAsset.objects.create(title="Diagram", kind=MediaKind.IMAGE)

        with pytest.raises(services.NotReadyToPublishError) as refused:
            services.publish(actor=publisher, instance=make_section(read_online, slug="signals", order=2, media=image))

        assert "put pictures in the text itself" in str(refused.value)

    def test_a_section_of_a_draft_book_is_refused(self, make_ebook, make_section, publisher):
        """Students could not reach it, as a subchapter of a draft chapter could not be reached."""
        draft = make_ebook(slug="unreleased", title="Unreleased", file=None)

        with pytest.raises(services.NotReadyToPublishError) as refused:
            services.publish(actor=publisher, instance=make_section(draft, slug="first"))

        assert "is not published" in str(refused.value)

    def test_a_sign_the_library_does_not_have_is_refused(self, make_section, read_online, publisher):
        with pytest.raises(services.NotReadyToPublishError) as refused:
            services.publish(
                actor=publisher,
                instance=make_section(read_online, slug="signals", body_html="<p>[signs: no-such-sign]</p>"),
            )

        assert "not in the library: no-such-sign" in str(refused.value)

    def test_the_text_is_cleaned_when_it_is_saved(self, make_section, read_online):
        section = make_section(read_online, slug="signals", order=2, body_html="<p>Safe</p><script>alert(1)</script>")

        assert "<script>" not in section.body_html


class TestTheListing:
    def test_a_student_sees_the_published_ebooks(self, api, student, live_ebook):
        live_ebook.cover.save("cover.png", uploaded_image("cover.png"), save=True)

        book = sign_in(api, student()).get(EBOOK_URL).data["ebooks"][0]

        assert book["slug"] == "pocket-guide"
        assert book["title"] == "Pocket guide"
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


class TestReadingOne:
    def test_a_student_gets_the_ebook_and_its_document(self, api, student, live_ebook):
        book = sign_in(api, student()).get(ebook_url("pocket-guide")).data["ebook"]

        assert book["slug"] == "pocket-guide"
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

        assert sign_in(api, browsing).get(ebook_url("pocket-guide")).status_code == 402


class TestReadingInSections:
    def test_the_book_comes_back_with_its_sections_and_their_blocks(self, api, student, read_online):
        response = sign_in(api, student()).get(ebook_url("study-guide"))

        assert response.data["ebook"]["title"] == "Study guide"
        assert [section["title"] for section in response.data["sections"]] == ["The hierarchy"]
        assert response.data["sections"][0]["number"] == 1
        assert response.data["sections"][0]["blocks"] == [
            {"type": "html", "html": "<p>Rule H1.</p>", "title": "The hierarchy"}
        ]

    def test_it_says_where_to_carry_on(self, api, student, read_online):
        response = sign_in(api, student()).get(ebook_url("study-guide"))

        assert response.data["current"] == "hierarchy"
        assert list(response.data) == ["ebook", "sections", "current"]

    def test_the_sections_are_served_in_the_order_staff_put_them_in(
        self, api, student, read_online, make_section, publisher
    ):
        for order, slug in ((3, "third"), (2, "second")):
            services.publish(
                actor=publisher, instance=make_section(read_online, slug=slug, title=slug.title(), order=order)
            )

        response = sign_in(api, student()).get(ebook_url("study-guide"))

        assert [section["slug"] for section in response.data["sections"]] == ["hierarchy", "second", "third"]
        assert [section["number"] for section in response.data["sections"]] == [1, 2, 3]

    def test_a_file_in_a_section_becomes_a_block_with_a_signed_link(
        self, api, student, read_online, make_section, publisher
    ):
        document = MediaAsset.objects.create(
            title="Rules",
            kind=MediaKind.DOCUMENT,
            file=SimpleUploadedFile("rules.pdf", b"%PDF-1.4", content_type="application/pdf"),
        )
        services.publish(
            actor=publisher,
            instance=make_section(read_online, slug="signals", title="Signals", order=2, media=document),
        )

        blocks = sign_in(api, student()).get(ebook_url("study-guide")).data["sections"][1]["blocks"]

        assert [block["type"] for block in blocks] == ["html", "document"]
        assert blocks[1]["url"] == document.file.url

    def test_a_video_in_a_section_becomes_a_video_block(self, api, student, read_online, make_section, publisher):
        video = MediaAsset.objects.create(
            title="Junctions",
            kind=MediaKind.VIDEO,
            file=SimpleUploadedFile("junctions.mp4", b"video", content_type="video/mp4"),
        )
        services.publish(
            actor=publisher,
            instance=make_section(read_online, slug="signals", order=2, body_html="", media=video),
        )

        blocks = sign_in(api, student()).get(ebook_url("study-guide")).data["sections"][1]["blocks"]

        assert [block["type"] for block in blocks] == ["video"]

    def test_signs_are_drawn_where_the_text_asks_for_them(
        self, api, student, read_online, sign, make_section, publisher
    ):
        services.publish(
            actor=publisher,
            instance=make_section(
                read_online,
                slug="warning-signs",
                title="Warning signs",
                order=2,
                body_html=(
                    "<p>Most are triangles.</p>"
                    "<p>[signs: give-way | Common warning signs]</p>"
                    "<p>A plate can say how far away it is.</p>"
                ),
            ),
        )

        blocks = sign_in(api, student()).get(ebook_url("study-guide")).data["sections"][1]["blocks"]

        assert [block["type"] for block in blocks] == ["html", "signs", "html"]
        assert blocks[0]["html"] == "<p>Most are triangles.</p>"
        assert blocks[1]["caption"] == "Common warning signs"
        assert blocks[1]["signs"][0]["name"] == "Give way"
        assert blocks[1]["signs"][0]["spec"]["shape"] == "inverted-triangle"
        assert blocks[2]["html"] == "<p>A plate can say how far away it is.</p>"


class TestReadingProgress:
    def read_url(self, slug: str) -> str:
        return reverse("learn-ebook-section-read", args=[slug])

    def test_a_student_can_mark_a_section_read(self, api, student, read_online):
        learner = sign_in(api, student())

        response = learner.post(self.read_url("hierarchy"))

        assert response.status_code == 200
        assert response.data == {"read": True}
        assert learner.get(ebook_url("study-guide")).data["sections"][0]["read"] is True

    def test_reading_it_twice_changes_nothing(self, api, student, read_online):
        learner = student()
        reader = sign_in(api, learner)
        reader.post(self.read_url("hierarchy"))
        reader.post(self.read_url("hierarchy"))

        assert EbookSectionProgress.objects.filter(student=learner).count() == 1

    def test_one_student_reading_does_not_mark_it_read_for_another(self, api, student, read_online):
        sign_in(api, student("starter")).post(self.read_url("hierarchy"))

        other = sign_in(APIClient(), student("premium"))

        assert other.get(ebook_url("study-guide")).data["sections"][0]["read"] is False

    def test_the_book_shows_which_sections_have_been_read(self, api, student, read_online, make_section, publisher):
        services.publish(actor=publisher, instance=make_section(read_online, slug="signals", title="Signals", order=2))
        learner = sign_in(api, student())
        learner.post(self.read_url("hierarchy"))

        sections = learner.get(ebook_url("study-guide")).data["sections"]

        assert [section["read"] for section in sections] == [True, False]

    def test_the_book_points_at_the_first_unread_section(self, api, student, read_online, make_section, publisher):
        services.publish(actor=publisher, instance=make_section(read_online, slug="signals", title="Signals", order=2))
        learner = sign_in(api, student())

        learner.post(self.read_url("hierarchy"))

        assert learner.get(ebook_url("study-guide")).data["current"] == "signals"
        assert learner.get(EBOOK_URL).data["ebooks"][0]["current"] == "signals"

    def test_a_day_spent_reading_a_book_counts_as_studying(self, api, student, read_online):
        learner = student()
        sign_in(api, learner).post(self.read_url("hierarchy"))

        assert services.streak(learner) == 1
        assert sign_in(api, learner).get(reverse("learn-progress")).data["sectionsRead"] == 1

    def test_an_unknown_section_is_not_found(self, api, student, read_online):
        assert sign_in(api, student()).post(self.read_url("no-such-section")).status_code == 404

    def test_somebody_without_a_plan_cannot_record_progress(self, api, read_online, db):
        browsing = User.objects.create_user(email="browsing@example.com", first_name="Bo")

        assert sign_in(api, browsing).post(self.read_url("hierarchy")).status_code == 402
        assert EbookSectionProgress.objects.count() == 0


def book_form(**overrides) -> dict:
    return {
        "title": "Road signs workbook",
        "slug": "road-signs-workbook",
        "description": "Every sign, with room to write.",
        "file": "",
        "order": 1,
        "publish_from": "",
        "sections-TOTAL_FORMS": 0,
        "sections-INITIAL_FORMS": 0,
        "sections-MIN_NUM_FORMS": 0,
        "sections-MAX_NUM_FORMS": 1000,
        **overrides,
    }


def section_form(**overrides) -> dict:
    return {
        "title": "Signals",
        "slug": "signals",
        "summary": "Who goes first.",
        "body_html": "<p>Rule 103.</p>",
        "media": "",
        "order": 2,
        "publish_from": "",
        **overrides,
    }


class TestTheAdminSection:
    """Two screens: the books, and the sections of them. Each is edited where it belongs."""

    def test_an_editor_can_create_a_book(self, editor_client, db):
        document = MediaAsset.objects.create(title="Workbook", kind=MediaKind.DOCUMENT)

        response = editor_client.post(reverse("admin:learning_ebook_add"), book_form(file=document.pk), follow=True)

        assert response.status_code == 200
        assert Ebook.objects.get(slug="road-signs-workbook").title == "Road signs workbook"

    def test_a_book_needs_no_document(self, editor_client, db):
        """The cover and the blurb are the book; the sections are written separately."""
        response = editor_client.post(reverse("admin:learning_ebook_add"), book_form(), follow=True)

        assert response.status_code == 200
        assert Ebook.objects.get(slug="road-signs-workbook").file is None

    def test_a_slug_already_in_use_is_refused_with_a_message(self, editor_client, make_ebook):
        make_ebook(slug="workbook", title="Workbook")

        response = editor_client.post(reverse("admin:learning_ebook_add"), book_form(slug="workbook"))

        assert "already exists" in response.content.decode()
        assert Ebook.objects.count() == 1

    def test_the_book_page_lists_its_sections_and_links_to_them(self, editor_client, read_online):
        page = editor_client.get(reverse("admin:learning_ebook_change", args=[read_online.pk])).content.decode()

        section = EbookSection.objects.get(slug="hierarchy")
        assert "Sections" in page
        assert reverse("admin:learning_ebooksection_change", args=[section.pk]) in page

    def test_a_publisher_can_publish_from_the_list(self, publisher_client, make_ebook):
        ebook = make_ebook()

        publisher_client.post(CHANGELIST, {"action": "publish_selected", "_selected_action": [ebook.pk]}, follow=True)

        ebook.refresh_from_db()
        assert ebook.status == PublishStatus.PUBLISHED


class TestTheSectionAdmin:
    changelist = reverse("admin:learning_ebooksection_changelist")

    def test_an_editor_can_write_one_and_attach_it_to_a_book(self, editor_client, read_online):
        response = editor_client.post(
            reverse("admin:learning_ebooksection_add"), section_form(ebook=read_online.pk), follow=True
        )

        assert response.status_code == 200
        section = EbookSection.objects.get(slug="signals")
        assert (section.ebook, section.order) == (read_online, 2)
        assert section.body_html == "<p>Rule 103.</p>"

    def test_the_page_offers_the_rich_text_editor(self, editor_client, read_online):
        page = editor_client.get(reverse("admin:learning_ebooksection_add")).content.decode()

        assert "django_ckeditor_5" in page

    def test_it_offers_text_and_a_file_but_not_questions_or_clips(self, editor_client, read_online):
        page = editor_client.get(reverse("admin:learning_ebooksection_add")).content.decode()

        assert 'name="body_html"' in page
        assert 'name="media"' in page
        assert 'name="question"' not in page
        assert 'name="hazard_clip"' not in page

    def test_it_explains_how_to_draw_signs_in_the_text(self, editor_client, read_online):
        page = editor_client.get(reverse("admin:learning_ebooksection_add")).content.decode()

        assert "[signs: give-way, stop | Common warning signs]" in page
        assert 'name="signs"' not in page

    def test_a_publisher_can_publish_one_from_the_list(self, publisher_client, read_online, make_section):
        section = make_section(read_online, slug="signals", title="Signals", order=2)

        publisher_client.post(
            self.changelist, {"action": "publish_selected", "_selected_action": [section.pk]}, follow=True
        )

        section.refresh_from_db()
        assert section.status == PublishStatus.PUBLISHED

    def test_an_editor_cannot_publish_one_from_the_list(self, editor_client, read_online, make_section):
        section = make_section(read_online, slug="signals", title="Signals", order=2)

        editor_client.post(
            self.changelist, {"action": "publish_selected", "_selected_action": [section.pk]}, follow=True
        )

        section.refresh_from_db()
        assert section.status == PublishStatus.DRAFT

    def test_reading_progress_is_shown_but_cannot_be_edited(self, editor_client, read_online, student, api):
        learner = student()
        sign_in(api, learner).post(reverse("learn-ebook-section-read", args=["hierarchy"]))

        page = editor_client.get(reverse("admin:learning_ebooksectionprogress_changelist")).content.decode()

        assert learner.email in page
        assert "Add ebook reading progress" not in page


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

    def test_it_lists_the_sections_of_a_book(self, editor_client, read_online):
        page = editor_client.get(self.preview_url(read_online)).content.decode()

        assert "Read online, a section at a time" in page
        assert "The hierarchy" in page
        assert "Nothing is published in here yet" not in page

    def test_a_section_is_previewed_as_a_student_would_read_it(self, editor_client, read_online):
        section = EbookSection.objects.get(slug="hierarchy")

        page = editor_client.get(reverse("admin:learning_ebooksection_preview", args=[section.pk])).content.decode()

        assert "<p>Rule H1.</p>" in page
        assert "Study guide" in page

    def test_it_still_reports_what_would_stop_publishing(self, editor_client, make_ebook):
        """The warning is the point of the page; showing the file must not have replaced it."""
        video = MediaAsset.objects.create(title="Clip", kind=MediaKind.VIDEO)

        page = editor_client.get(self.preview_url(make_ebook(file=video))).content.decode()

        assert "This cannot be published yet" in page
        assert "not a document" in page
