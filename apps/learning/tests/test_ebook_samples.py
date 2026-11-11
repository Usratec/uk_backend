import pytest
from django.core import mail
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.urls import reverse

from apps.accounts.models import User
from apps.learning import roles, services
from apps.learning.admin import EbookSectionForm
from apps.learning.models import EbookSampleRequest, EbookSection
from apps.learning.tests.conftest import PASSWORD, sign_in, staff_member

pytestmark = pytest.mark.django_db

OUTLINE_URL = reverse("learn-ebook-public")
REQUEST_URL = reverse("learn-ebook-sample-request")
EBOOK_URL = reverse("learn-ebook")
LEADS_URL = reverse("admin:learning_ebooksamplerequest_changelist")

SAMPLE_BYTES = b"%PDF-1.4 the first chapter, free to all"

BOOK_KEYS = {"slug", "title", "description", "sections"}
SECTION_KEYS = {"slug", "number", "title", "summary", "isFreeSample", "samplePdfUrl"}


@pytest.fixture(autouse=True)
def clear_throttle_counters():
    cache.clear()
    yield
    cache.clear()


def sample_url(slug: str) -> str:
    return reverse("learn-ebook-sample", args=[slug])


def sample_pdf(name: str = "chapter-one.pdf", content: bytes = SAMPLE_BYTES) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, content, content_type="application/pdf")


@pytest.fixture
def landing_page(make_ebook, make_section, publisher):
    book = make_ebook(slug="study-guide", title="Study guide", description="Every rule, with room to write.")
    first = make_section(
        book,
        slug="chapter-one",
        title="Introduction",
        summary="Learn the fundamentals.",
        order=1,
        is_free_sample=True,
        sample_pdf=sample_pdf(),
    )
    second = make_section(
        book,
        slug="chapter-two",
        title="Advanced concepts",
        summary="Explore advanced concepts.",
        order=2,
        body_html="<p>Stopping distances in the wet.</p>",
    )

    services.publish(actor=publisher, instance=book)
    for section in (first, second):
        services.publish(actor=publisher, instance=section)

    book.refresh_from_db()
    return book


class TestTheOutline:
    def test_a_visitor_with_no_account_can_read_it(self, api, landing_page):
        response = api.get(OUTLINE_URL)

        assert response.status_code == 200
        assert [book["title"] for book in response.data["ebooks"]] == ["Study guide"]

    def test_only_the_permitted_book_fields_are_returned(self, api, landing_page):
        book = api.get(OUTLINE_URL).data["ebooks"][0]

        assert set(book) == BOOK_KEYS

    def test_only_the_permitted_chapter_fields_are_returned(self, api, landing_page):
        chapters = api.get(OUTLINE_URL).data["ebooks"][0]["sections"]

        assert [set(chapter) for chapter in chapters] == [SECTION_KEYS, SECTION_KEYS]

    def test_the_chapters_come_back_numbered_in_reading_order(self, api, landing_page):
        chapters = api.get(OUTLINE_URL).data["ebooks"][0]["sections"]

        assert [(chapter["number"], chapter["slug"]) for chapter in chapters] == [
            (1, "chapter-one"),
            (2, "chapter-two"),
        ]

    def test_the_chapter_staff_flagged_is_the_one_marked_free(self, api, landing_page):
        chapters = api.get(OUTLINE_URL).data["ebooks"][0]["sections"]

        assert [chapter["isFreeSample"] for chapter in chapters] == [True, False]
        assert chapters[0]["samplePdfUrl"] == f"http://testserver{sample_url('chapter-one')}"
        assert chapters[1]["samplePdfUrl"] is None

    def test_the_chapter_text_is_never_included(self, api, landing_page):
        assert "Stopping distances in the wet." not in str(api.get(OUTLINE_URL).data)

    def test_the_books_own_document_is_never_named(self, api, landing_page):
        body = str(api.get(OUTLINE_URL).data)

        assert landing_page.file.file.name not in body
        assert "X-Amz" not in body
        assert "sample_pdf" not in body

    def test_nothing_about_any_student_is_included(self, api, student, landing_page):
        reader = student()
        services.mark_section_read(reader, EbookSection.objects.get(slug="chapter-one"))

        shelf = api.get(OUTLINE_URL).data

        assert "read" not in shelf["ebooks"][0]["sections"][0]
        assert "current" not in shelf["ebooks"][0]
        assert reader.email not in str(shelf)

    def test_drafts_are_left_out_entirely(self, api, make_ebook, make_section, landing_page):
        unreleased = make_ebook(slug="unreleased", title="Unreleased")
        make_section(unreleased, slug="secret", title="Secret")
        draft_chapter = make_section(landing_page, slug="chapter-three", title="Not ready yet", order=3)

        shelf = api.get(OUTLINE_URL).data["ebooks"]

        assert [book["slug"] for book in shelf] == ["study-guide"]
        assert draft_chapter.slug not in [chapter["slug"] for chapter in shelf[0]["sections"]]

    def test_the_whole_shelf_costs_two_queries(
        self, api, landing_page, make_ebook, make_section, publisher, django_assert_num_queries
    ):
        for index in range(3):
            book = make_ebook(slug=f"extra-{index}", title=f"Extra {index}", order=index + 2)
            chapter = make_section(book, slug=f"extra-{index}-one", title="One")
            services.publish(actor=publisher, instance=book)
            services.publish(actor=publisher, instance=chapter)

        with django_assert_num_queries(2):
            api.get(OUTLINE_URL)


class TestTheSampleDownload:
    def test_a_visitor_with_no_account_can_download_it(self, api, landing_page):
        response = api.get(sample_url("chapter-one"))

        assert response.status_code == 200
        assert b"".join(response.streaming_content) == SAMPLE_BYTES

    def test_it_comes_back_as_a_pdf_to_download(self, api, landing_page):
        response = api.get(sample_url("chapter-one"))

        assert response["Content-Type"] == "application/pdf"
        assert response["Content-Disposition"] == 'attachment; filename="chapter-one.pdf"'
        assert response["X-Content-Type-Options"] == "nosniff"

    @pytest.mark.parametrize("accept", ["application/pdf", "text/html", "*/*"])
    def test_it_is_served_whatever_the_client_asks_for(self, api, landing_page, accept):
        response = api.get(sample_url("chapter-one"), headers={"accept": accept})

        assert response.status_code == 200
        assert response["Content-Type"] == "application/pdf"

    def test_a_missing_sample_is_still_json_when_a_pdf_was_asked_for(self, api, landing_page):
        response = api.get(sample_url("chapter-two"), headers={"accept": "application/pdf"})

        assert response.status_code == 404
        assert response.json() == {"detail": "Not found."}

    def test_it_is_the_sample_file_and_not_the_book(self, api, landing_page):
        served = b"".join(api.get(sample_url("chapter-one")).streaming_content)

        assert served == SAMPLE_BYTES
        assert served != landing_page.file.file.read()

    def test_a_chapter_nobody_flagged_cannot_be_downloaded(self, api, landing_page):
        assert api.get(sample_url("chapter-two")).status_code == 404

    def test_a_flagged_chapter_with_no_pdf_cannot_be_downloaded(self, api, make_ebook, make_section, publisher):
        book = make_ebook(slug="almanac", title="Almanac")
        chapter = make_section(book, slug="opening", title="Opening", is_free_sample=True)
        services.publish(actor=publisher, instance=book)
        services.publish(actor=publisher, instance=chapter)

        assert api.get(sample_url("opening")).status_code == 404

    def test_a_chapter_of_a_draft_book_cannot_be_downloaded(self, api, make_ebook, make_section):
        book = make_ebook(slug="unreleased", title="Unreleased")
        make_section(book, slug="secret", title="Secret", is_free_sample=True, sample_pdf=sample_pdf())

        assert api.get(sample_url("secret")).status_code == 404

    def test_an_unpublished_chapter_cannot_be_downloaded(self, api, make_ebook, make_section, publisher):
        book = make_ebook(slug="almanac", title="Almanac")
        make_section(book, slug="draft-sample", title="Draft", is_free_sample=True, sample_pdf=sample_pdf())
        services.publish(actor=publisher, instance=book)

        assert api.get(sample_url("draft-sample")).status_code == 404

    def test_an_unknown_slug_is_not_found(self, api, landing_page):
        response = api.get(sample_url("no-such-chapter"))

        assert response.status_code == 404
        assert response.json() == {"detail": "Not found."}

    @pytest.mark.parametrize(
        "attempt",
        [
            "..%2F..%2F..%2Fetc%2Fpasswd",
            "chapter-one%2F..%2Fchapter-two",
        ],
    )
    def test_a_manipulated_identifier_reaches_nothing(self, api, landing_page, attempt):
        assert api.get(f"/api/learn/ebook/public/samples/{attempt}/").status_code == 404


class TestWhatStaysProtected:
    def test_the_student_shelf_still_needs_a_token(self, api, landing_page):
        assert api.get(EBOOK_URL).status_code == 401

    def test_somebody_without_a_plan_still_cannot_read_the_book(self, api, landing_page):
        browsing = User.objects.create_user(email="browsing@example.com", password=PASSWORD, first_name="Bo")

        response = sign_in(api, browsing).get(reverse("learn-ebook-detail", args=["study-guide"]))

        assert response.status_code == 402

    def test_a_buyer_still_gets_the_signed_link_to_the_whole_book(self, api, student, landing_page):
        response = sign_in(api, student()).get(reverse("learn-ebook-detail", args=["study-guide"]))

        assert response.status_code == 200
        assert response.data["ebook"]["url"] == landing_page.file.file.url
        assert response.data["ebook"]["contentType"] == "application/pdf"

    def test_the_students_own_shelf_is_unchanged(self, api, student, landing_page):
        book = sign_in(api, student()).get(EBOOK_URL).data["ebooks"][0]

        assert {"slug", "title", "description", "coverUrl", "updatedAt", "hasDocument", "sections", "current"} <= set(
            book
        )
        assert book["sections"][0]["read"] is False


class TestDesignatingTheSample:
    def test_a_book_cannot_give_two_chapters_away(self, make_section, landing_page):
        with pytest.raises(IntegrityError), transaction.atomic():
            make_section(
                landing_page,
                slug="chapter-four",
                title="Also free",
                order=4,
                is_free_sample=True,
                sample_pdf=sample_pdf("chapter-four.pdf"),
            )

    def test_a_sample_that_is_not_a_pdf_is_refused(self, make_section, make_ebook):
        book = make_ebook(slug="almanac", title="Almanac")

        with pytest.raises(ValidationError):
            make_section(
                book,
                slug="opening",
                title="Opening",
                is_free_sample=True,
                sample_pdf=SimpleUploadedFile("opening.pdf", b"GIF89a not a pdf", content_type="application/pdf"),
            )

    def test_the_admin_form_refuses_the_flag_without_a_file(self, landing_page):
        form = EbookSectionForm(
            data={
                "ebook": landing_page.pk,
                "title": "Also free",
                "slug": "chapter-five",
                "summary": "",
                "body_html": "<p>Something.</p>",
                "order": 5,
                "is_free_sample": True,
            }
        )

        assert not form.is_valid()
        assert "sample_pdf" in form.errors


class TestAskingForTheSampleByEmail:
    def test_a_visitor_is_sent_the_public_link(self, api, landing_page):
        response = api.post(REQUEST_URL, {"email": "visitor@example.com", "ebookSlug": "study-guide"}, format="json")

        assert response.status_code == 201
        assert response.data["samplePdfUrl"] == f"http://testserver{sample_url('chapter-one')}"
        assert len(mail.outbox) == 1
        assert sample_url("chapter-one") in mail.outbox[0].body

    def test_the_email_never_carries_the_whole_book(self, api, landing_page):
        api.post(REQUEST_URL, {"email": "visitor@example.com", "ebookSlug": "study-guide"}, format="json")

        message = mail.outbox[0]
        sent = str(message.body) + str(getattr(message, "alternatives", []))

        assert landing_page.file.file.name not in sent
        assert "X-Amz" not in sent

    def test_consent_is_recorded_when_it_is_given(self, api, landing_page):
        api.post(
            REQUEST_URL,
            {"email": "visitor@example.com", "ebookSlug": "study-guide", "studyTipsConsent": True},
            format="json",
        )

        asked = EbookSampleRequest.objects.get()

        assert asked.study_tips_consent is True
        assert asked.consent_at is not None

    def test_a_missing_tick_box_is_not_consent(self, api, landing_page):
        api.post(REQUEST_URL, {"email": "visitor@example.com", "ebookSlug": "study-guide"}, format="json")

        asked = EbookSampleRequest.objects.get()

        assert asked.study_tips_consent is False
        assert asked.consent_at is None

    def test_the_snake_case_spelling_of_the_form_is_accepted_too(self, api, landing_page):
        response = api.post(
            REQUEST_URL,
            {"email": "visitor@example.com", "ebook_slug": "study-guide", "study_tips_consent": True},
            format="json",
        )

        assert response.status_code == 201
        assert EbookSampleRequest.objects.get().study_tips_consent is True

    def test_an_invalid_address_is_refused(self, api, landing_page):
        response = api.post(REQUEST_URL, {"email": "not-an-address", "ebookSlug": "study-guide"}, format="json")

        assert response.status_code == 400
        assert mail.outbox == []
        assert EbookSampleRequest.objects.count() == 0

    def test_a_book_nobody_has_published_is_not_found(self, api, make_ebook, landing_page):
        make_ebook(slug="unreleased", title="Unreleased")

        response = api.post(REQUEST_URL, {"email": "visitor@example.com", "ebookSlug": "unreleased"}, format="json")

        assert response.status_code == 404
        assert mail.outbox == []

    def test_a_book_with_no_free_chapter_says_so(self, api, make_ebook, make_section, publisher):
        book = make_ebook(slug="almanac", title="Almanac")
        chapter = make_section(book, slug="opening", title="Opening")
        services.publish(actor=publisher, instance=book)
        services.publish(actor=publisher, instance=chapter)

        response = api.post(REQUEST_URL, {"email": "visitor@example.com", "ebookSlug": "almanac"}, format="json")

        assert response.status_code == 409
        assert mail.outbox == []
        assert EbookSampleRequest.objects.count() == 0

    def test_the_download_needs_no_form_and_no_consent(self, api, landing_page):
        assert api.get(sample_url("chapter-one")).status_code == 200
        assert EbookSampleRequest.objects.count() == 0

    def test_the_eleventh_request_from_one_address_is_throttled(self, api, landing_page):
        for attempt in range(10):
            sent = api.post(
                REQUEST_URL, {"email": f"visitor{attempt}@example.com", "ebookSlug": "study-guide"}, format="json"
            )
            assert sent.status_code == 201

        response = api.post(
            REQUEST_URL, {"email": "one-too-many@example.com", "ebookSlug": "study-guide"}, format="json"
        )

        assert response.status_code == 429
        assert len(mail.outbox) == 10
        assert EbookSampleRequest.objects.count() == 10

    def test_the_throttle_counts_requests_that_sent_nothing(self, api, landing_page):
        for _ in range(10):
            api.post(REQUEST_URL, {"email": "not-an-address", "ebookSlug": "study-guide"}, format="json")

        response = api.post(REQUEST_URL, {"email": "visitor@example.com", "ebookSlug": "study-guide"}, format="json")

        assert response.status_code == 429
        assert mail.outbox == []


class TestWhoCanSeeTheLeads:
    def test_writing_content_does_not_come_with_the_addresses(self, client, editor):
        client.force_login(editor)

        assert client.get(LEADS_URL).status_code == 403

    def test_releasing_content_does_not_come_with_them_either(self, client, publisher):
        client.force_login(publisher)

        assert client.get(LEADS_URL).status_code == 403

    def test_the_support_role_can_read_them(self, client, db, landing_page):
        support = staff_member("support@example.com", "Marketing Support")
        client.force_login(support)

        response = client.get(LEADS_URL)

        assert response.status_code == 200

    def test_the_support_role_cannot_change_or_delete_them(self, client, db, landing_page):
        asked = EbookSampleRequest.objects.create(ebook=landing_page, email="visitor@example.com")
        client.force_login(staff_member("support@example.com", "Marketing Support"))

        change = client.get(reverse("admin:learning_ebooksamplerequest_change", args=[asked.pk]))
        deletion = client.post(reverse("admin:learning_ebooksamplerequest_delete", args=[asked.pk]), {"post": "yes"})

        assert b"_save" not in change.content
        assert deletion.status_code == 403
        assert EbookSampleRequest.objects.count() == 1

    def test_the_support_role_carries_nothing_but_the_addresses(self):
        assert roles.MARKETING_SUPPORT.permissions == ("learning.view_ebooksamplerequest",)

    def test_no_content_role_declares_them(self):
        for role in (roles.CONTENT_EDITOR, roles.CONTENT_PUBLISHER):
            assert "learning.view_ebooksamplerequest" not in role.permissions
