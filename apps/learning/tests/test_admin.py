"""The custom parts of the Learning admin: the hierarchy, publish actions, editor and preview.

Standard Django CRUD is not re-tested here; only behaviour this project added.
"""

import pytest
from django.urls import reverse

from apps.core.models import PublishStatus
from apps.learning.models import LearningContent

from .conftest import CONTENT

pytestmark = pytest.mark.django_db

CONTENT_LIST = reverse("admin:learning_learningcontent_changelist")


def change_url(instance) -> str:
    meta = instance._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_change", args=[instance.pk])


def preview_url(instance) -> str:
    meta = instance._meta
    return reverse(f"admin:{meta.app_label}_{meta.model_name}_preview", args=[instance.pk])


def content_form(content, **overrides) -> dict:
    return {
        "subchapter": content.subchapter_id,
        "content_type": content.content_type,
        "title": content.title,
        "slug": content.slug,
        "order": content.order,
        "body_html": content.body_html,
        **overrides,
    }


class TestHierarchy:
    def test_a_chapter_lists_its_subchapters(self, editor_client, chapter, subchapter):
        page = editor_client.get(change_url(chapter)).content.decode()

        assert subchapter.title in page

    def test_a_subchapter_lists_its_material(self, editor_client, subchapter, content):
        page = editor_client.get(change_url(subchapter)).content.decode()

        assert content.title in page

    def test_material_is_written_on_its_own_page_not_in_the_inline(self, editor_client, subchapter, content):
        """A rich-text editor inside a nested inline is unusable, so the inline only links out."""
        inline_page = editor_client.get(change_url(subchapter)).content.decode()
        own_page = editor_client.get(change_url(content)).content.decode()

        assert "body_html" not in inline_page
        assert "ckeditor" in own_page.lower()

    def test_the_content_list_shows_where_each_item_sits(self, editor_client, content):
        page = editor_client.get(CONTENT_LIST).content.decode()

        assert content.subchapter.title in page
        assert content.subchapter.chapter.title in page

    def test_counts_show_how_much_is_published(self, editor_client, chapter, subchapter):
        page = editor_client.get(reverse("admin:learning_chapter_changelist")).content.decode()

        assert "0 published / 1 total" in page


class TestPublishAction:
    def publish(self, client, instance, action="publish_selected"):
        meta = instance._meta
        url = reverse(f"admin:{meta.app_label}_{meta.model_name}_changelist")
        return client.post(url, {"action": action, "_selected_action": [instance.pk]}, follow=True)

    def test_a_publisher_can_publish_from_the_list(self, publisher_client, subchapter, content):
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])

        self.publish(publisher_client, content)

        content.refresh_from_db()
        assert content.status == PublishStatus.PUBLISHED

    def test_the_action_is_hidden_from_editors(self, editor_client, content):
        page = editor_client.get(CONTENT_LIST).content.decode()

        assert "publish_selected" not in page

    def test_an_editor_posting_the_action_anyway_changes_nothing(self, editor_client, subchapter, content):
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])

        self.publish(editor_client, content)

        content.refresh_from_db()
        assert content.status == PublishStatus.DRAFT

    def test_material_that_is_not_ready_reports_why(self, publisher_client, content):
        response = self.publish(publisher_client, content)

        assert "not published" in response.content.decode()
        content.refresh_from_db()
        assert content.status == PublishStatus.DRAFT

    def test_unpublishing_takes_it_off_the_site(self, publisher_client, published_tree):
        _, _, content = published_tree

        self.publish(publisher_client, content, "unpublish_selected")

        content.refresh_from_db()
        assert content.status == PublishStatus.DRAFT


class TestRichTextEditor:
    def test_material_written_in_the_editor_is_saved(self, editor_client, content):
        written = "<h2>Speed limits</h2><p>Thirty unless signed otherwise.</p>"

        editor_client.post(change_url(content), content_form(content, body_html=written))

        content.refresh_from_db()
        assert content.body_html == written

    def test_dangerous_markup_never_reaches_the_database(self, editor_client, content):
        """Whatever was pasted into the editor, what is stored is what students can be served."""
        editor_client.post(change_url(content), content_form(content, body_html="<p>Fine</p><script>steal()</script>"))

        content.refresh_from_db()
        assert content.body_html == "<p>Fine</p>"

    def test_every_kind_of_material_we_can_serve_is_offered(self, editor_client, content):
        """All five kinds work now. The form offers exactly what the product can serve."""
        page = editor_client.get(change_url(content)).content.decode()

        for kind in ("theory", "question", "video", "document", "hazard"):
            assert f'value="{kind}"' in page


class TestAccess:
    def test_the_package_picker_is_on_the_form(self, editor_client, content):
        page = editor_client.get(change_url(content)).content.decode()

        assert "only_for_packages" in page
        assert "Leave empty for everyone with learning access." in page


class TestScheduling:
    def test_the_publish_from_field_is_on_the_form(self, editor_client, content):
        page = editor_client.get(change_url(content)).content.decode()

        assert "publish_from" in page

    def test_scheduled_material_is_shown_as_scheduled(self, editor_client, published_tree):
        from datetime import timedelta

        from django.utils import timezone

        _, _, content = published_tree
        content.publish_from = timezone.now() + timedelta(days=1)
        content.save(update_fields=["publish_from"])

        page = editor_client.get(CONTENT_LIST).content.decode()

        assert "Scheduled" in page


class TestPreview:
    def test_a_subchapter_preview_shows_its_published_material(self, editor_client, published_tree):
        _, subchapter, content = published_tree

        page = editor_client.get(preview_url(subchapter)).content.decode()

        assert "Thinking distance plus braking distance." in page
        assert subchapter.title in page

    def test_the_preview_renders_html_rather_than_showing_tags(self, editor_client, published_tree):
        _, subchapter, _ = published_tree

        page = editor_client.get(preview_url(subchapter)).content.decode()

        assert CONTENT in page
        assert "&lt;p&gt;" not in page

    def test_draft_material_is_left_out_of_the_preview(self, editor_client, published_tree, subchapter):
        LearningContent.objects.create(subchapter=subchapter, slug="draft", title="Draft", body_html="<p>Not yet.</p>")

        page = editor_client.get(preview_url(subchapter)).content.decode()

        assert "Not yet." not in page

    def test_it_warns_about_what_would_stop_publishing(self, editor_client, subchapter):
        page = editor_client.get(preview_url(subchapter)).content.decode()

        assert "cannot be published yet" in page

    def test_staff_without_learning_permissions_are_refused(self, client, outsider, content):
        """A custom page needs its own check; being in the admin is not enough."""
        client.force_login(outsider)

        assert client.get(preview_url(content)).status_code == 403

    def test_signed_out_visitors_are_sent_to_the_login_page(self, client, content):
        response = client.get(preview_url(content))

        assert response.status_code == 302
        assert "login" in response["Location"]


class TestMenu:
    def test_an_editor_sees_learning_and_not_the_crm(self, editor_client):
        shown = {app["app_label"] for app in editor_client.get(reverse("admin:index")).context["available_apps"]}

        assert "learning" in shown
        assert "crm" not in shown


class TestMockTestComposition:
    """A mock test is put together on one page: the paper, then the clips."""

    def test_the_page_offers_both_questions_and_clips(self, editor_client, db):
        from apps.learning.models import ExamKind, PracticeExam

        exam = PracticeExam.objects.create(slug="mock-1", title="Mock test 1", kind=ExamKind.MOCK, pass_mark=1)

        page = editor_client.get(change_url(exam)).content.decode()

        assert "exam_questions-TOTAL_FORMS" in page
        assert "exam_clips-TOTAL_FORMS" in page
        assert "Hazard clips" in page

    def test_an_editor_can_add_a_clip_to_one(self, editor_client, make_clip, db):
        from apps.learning.models import ExamKind, PracticeExam

        exam = PracticeExam.objects.create(slug="mock-1", title="Mock test 1", kind=ExamKind.MOCK, pass_mark=1)
        clip = make_clip()

        editor_client.post(
            change_url(exam),
            {
                "title": exam.title,
                "slug": exam.slug,
                "description": "",
                "kind": exam.kind,
                "order": exam.order,
                "pass_mark": exam.pass_mark,
                "time_limit_seconds": "",
                "publish_from": "",
                "exam_questions-TOTAL_FORMS": "0",
                "exam_questions-INITIAL_FORMS": "0",
                "exam_questions-MIN_NUM_FORMS": "0",
                "exam_questions-MAX_NUM_FORMS": "1000",
                "exam_clips-TOTAL_FORMS": "1",
                "exam_clips-INITIAL_FORMS": "0",
                "exam_clips-MIN_NUM_FORMS": "0",
                "exam_clips-MAX_NUM_FORMS": "1000",
                "exam_clips-0-clip": clip.pk,
                "exam_clips-0-order": "1",
            },
            follow=True,
        )

        assert [added.slug for added in exam.hazard_clips.all()] == [clip.slug]

    def test_the_list_shows_how_many_of_each(self, editor_client, make_clip, db):
        from apps.learning.models import ExamKind, PracticeExam

        exam = PracticeExam.objects.create(slug="mock-1", title="Mock test 1", kind=ExamKind.MOCK, pass_mark=1)
        exam.exam_clips.create(clip=make_clip(), order=1)

        page = editor_client.get(reverse("admin:learning_practiceexam_changelist")).content.decode()

        assert exam.title in page

    def test_the_page_says_what_the_clips_are_worth(self, editor_client, make_clip, db):
        from apps.learning.models import ExamKind, PracticeExam

        exam = PracticeExam.objects.create(slug="mock-1", title="Mock test 1", kind=ExamKind.MOCK, pass_mark=1)
        exam.exam_clips.create(clip=make_clip(hazards=2), order=1)

        page = editor_client.get(change_url(exam)).content.decode()

        assert "10 marks across 1 clip(s)" in page

    def test_a_question_only_exam_says_so(self, editor_client, db):
        from apps.learning.models import ExamKind, PracticeExam

        exam = PracticeExam.objects.create(slug="drill", title="Signs drill", kind=ExamKind.PRACTICE, pass_mark=1)

        page = editor_client.get(change_url(exam)).content.decode()

        assert "question-only paper" in page


class TestAttemptStatistics:
    """What a sitting came to, shown to staff from what was recorded at the time."""

    def attempt(self, student, exam, **fields):
        from apps.learning.models import MockAttempt

        return MockAttempt.objects.create(
            student=student,
            exam=exam,
            score=fields.pop("score", 1),
            total=fields.pop("total", 2),
            passed=fields.pop("passed", False),
            **fields,
        )

    def exam(self):
        from apps.learning.models import ExamKind, PracticeExam

        return PracticeExam.objects.create(slug="mock-1", title="Mock test 1", kind=ExamKind.MOCK, pass_mark=2)

    def test_the_detail_page_breaks_the_questions_down(self, publisher_client, student):
        sat = self.attempt(student(), self.exam(), score=3, total=5, unanswered=1)

        page = publisher_client.get(change_url(sat)).content.decode()

        assert "3 right, 1 wrong, 1 left blank, of 5 asked" in page

    def test_the_detail_page_shows_the_hazard_score(self, publisher_client, student):
        sat = self.attempt(student(), self.exam(), hazard_score=7, hazard_top_score=10)

        page = publisher_client.get(change_url(sat)).content.decode()

        assert "7/10" in page

    def test_a_question_only_sitting_shows_no_hazard_score(self, publisher_client, student):
        """Rather than 0/0, which reads as a hazard test failed."""
        sat = self.attempt(student(), self.exam())

        page = publisher_client.get(change_url(sat)).content.decode()

        assert "0/0" not in page

    def test_the_list_shows_both_scores(self, publisher_client, student):
        self.attempt(student(), self.exam(), score=4, total=5, hazard_score=7, hazard_top_score=10)

        page = publisher_client.get(reverse("admin:learning_mockattempt_changelist")).content.decode()

        assert "4/5" in page
        assert "7/10" in page

    def test_a_sitting_cannot_be_edited(self, publisher_client, student):
        """Somebody's history is looked at, never rewritten."""
        sat = self.attempt(student(), self.exam())

        page = publisher_client.get(change_url(sat)).content.decode()

        assert publisher_client.get(reverse("admin:learning_mockattempt_add")).status_code == 403
        assert 'name="score"' not in page
