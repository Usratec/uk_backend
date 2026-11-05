import pytest
from django.contrib.auth.models import Group, Permission
from django.urls import reverse

from apps.learning.models import (
    ExamHazardClip,
    ExamKind,
    ExamQuestion,
    PracticeExam,
    Subtitle,
    SubtitleLanguage,
)
from apps.learning.roles import CONTENT_MODELS, EDITING, INLINE_MODELS, PROGRESS_MODELS
from apps.staff.access import granted_permissions

from .conftest import staff_member
from .test_subtitles import track, video

pytestmark = pytest.mark.django_db

STANDALONE_MODELS = tuple(model for model in CONTENT_MODELS if model not in INLINE_MODELS)


def permissions_of(role: str) -> set[str]:
    rows = Group.objects.get(name=role).permissions.values_list("content_type__app_label", "codename")
    return {f"{app_label}.{codename}" for app_label, codename in rows}


def exam_with_two_of_each(make_question, make_clip) -> PracticeExam:
    exam = PracticeExam.objects.create(slug="practice-1", title="Practice 1", kind=ExamKind.PRACTICE, pass_mark=1)
    exam.exam_questions.create(question=make_question(key="distraction"), order=1)
    exam.exam_questions.create(question=make_question(key="stopping"), order=2)
    exam.exam_clips.create(clip=make_clip(slug="country-lane", title="Country lane"), order=1)
    exam.exam_clips.create(clip=make_clip(slug="town-centre", title="Town centre"), order=2)
    return exam


def exam_form(exam: PracticeExam, **overrides) -> dict:
    questions = list(exam.exam_questions.order_by("order"))
    clips = list(exam.exam_clips.order_by("order"))
    data: dict = {
        "title": exam.title,
        "slug": exam.slug,
        "description": exam.description,
        "kind": exam.kind,
        "order": exam.order,
        "pass_mark": exam.pass_mark,
        "time_limit_seconds": "",
        "hazard_pass_mark": "",
        "publish_from": "",
        "exam_questions-TOTAL_FORMS": str(len(questions)),
        "exam_questions-INITIAL_FORMS": str(len(questions)),
        "exam_questions-MIN_NUM_FORMS": "0",
        "exam_questions-MAX_NUM_FORMS": "1000",
        "exam_clips-TOTAL_FORMS": str(len(clips)),
        "exam_clips-INITIAL_FORMS": str(len(clips)),
        "exam_clips-MIN_NUM_FORMS": "0",
        "exam_clips-MAX_NUM_FORMS": "1000",
    }
    for index, place in enumerate(questions):
        data |= {
            f"exam_questions-{index}-id": place.pk,
            f"exam_questions-{index}-exam": exam.pk,
            f"exam_questions-{index}-question": place.question_id,
            f"exam_questions-{index}-order": place.order,
        }
    for index, shown in enumerate(clips):
        data |= {
            f"exam_clips-{index}-id": shown.pk,
            f"exam_clips-{index}-exam": exam.pk,
            f"exam_clips-{index}-clip": shown.clip_id,
            f"exam_clips-{index}-order": shown.order,
        }
    return data | overrides


def asset_form(asset, **overrides) -> dict:
    tracks = list(asset.subtitles.all())
    data: dict = {
        "title": asset.title,
        "kind": asset.kind,
        "subtitles-TOTAL_FORMS": str(len(tracks) + 1),
        "subtitles-INITIAL_FORMS": str(len(tracks)),
        "subtitles-MIN_NUM_FORMS": "0",
        "subtitles-MAX_NUM_FORMS": "1000",
    }
    for index, existing in enumerate(tracks):
        data |= {
            f"subtitles-{index}-id": existing.pk,
            f"subtitles-{index}-asset": asset.pk,
            f"subtitles-{index}-language": existing.language,
        }
    return data | overrides


def exam_url(exam: PracticeExam) -> str:
    return reverse("admin:learning_practiceexam_change", args=[exam.pk])


def asset_url(asset) -> str:
    return reverse("admin:learning_mediaasset_change", args=[asset.pk])


@pytest.fixture
def editor_without_delete(db):
    group = Group.objects.create(name="Content Editor (no removing)")
    for permission in EDITING:
        app_label, codename = permission.split(".")
        group.permissions.add(Permission.objects.get(content_type__app_label=app_label, codename=codename))
    editor = staff_member("careful@example.com", None)
    editor.groups.add(group)
    return editor


@pytest.fixture
def no_delete_client(client, editor_without_delete):
    client.force_login(editor_without_delete)
    return client


class TestTheRolesCarryDelete:
    @pytest.mark.parametrize("model", INLINE_MODELS)
    @pytest.mark.parametrize("role", ["Content Editor", "Content Publisher"])
    def test_an_inline_row_may_be_removed(self, role, model):
        assert f"learning.delete_{model}" in permissions_of(role)

    @pytest.mark.parametrize("role", ["Content Editor", "Content Publisher"])
    def test_writing_material_is_unchanged(self, role):
        assert set(EDITING) <= permissions_of(role)

    @pytest.mark.parametrize("model", STANDALONE_MODELS)
    @pytest.mark.parametrize("role", ["Content Editor", "Content Publisher"])
    def test_a_record_with_a_screen_of_its_own_still_cannot_be_deleted(self, role, model):
        assert f"learning.delete_{model}" not in permissions_of(role)

    @pytest.mark.parametrize("model", PROGRESS_MODELS)
    @pytest.mark.parametrize("role", ["Content Editor", "Content Publisher"])
    def test_what_students_have_done_still_cannot_be_deleted(self, role, model):
        assert f"learning.delete_{model}" not in permissions_of(role)

    def test_only_a_publisher_may_still_release_material(self):
        assert "learning.publish_practiceexam" in permissions_of("Content Publisher")
        assert "learning.publish_practiceexam" not in permissions_of("Content Editor")

    def test_no_other_role_was_widened(self):
        assert not any(name.startswith("learning.") for name in permissions_of("CRM Manager"))

    def test_staff_without_the_role_hold_nothing(self, outsider):
        assert granted_permissions(outsider) == frozenset()


class TestTheExamPage:
    def test_a_saved_question_offers_the_delete_box(self, editor_client, make_question, make_clip):
        exam = exam_with_two_of_each(make_question, make_clip)

        page = editor_client.get(exam_url(exam)).content.decode()

        assert "exam_questions-0-DELETE" in page

    def test_a_saved_clip_offers_the_delete_box(self, editor_client, make_question, make_clip):
        exam = exam_with_two_of_each(make_question, make_clip)

        page = editor_client.get(exam_url(exam)).content.decode()

        assert "exam_clips-0-DELETE" in page

    def test_an_editor_removes_a_question_and_the_rest_stay(self, editor_client, make_question, make_clip):
        exam = exam_with_two_of_each(make_question, make_clip)
        removed = exam.exam_questions.order_by("order")[0]

        response = editor_client.post(
            exam_url(exam), exam_form(exam, **{"exam_questions-0-DELETE": "on"}), follow=True
        )

        assert response.status_code == 200
        assert not ExamQuestion.objects.filter(pk=removed.pk).exists()
        assert exam.exam_questions.count() == 1

    def test_an_editor_removes_a_clip_and_the_rest_stay(self, editor_client, make_question, make_clip):
        exam = exam_with_two_of_each(make_question, make_clip)
        removed = exam.exam_clips.order_by("order")[0]

        response = editor_client.post(exam_url(exam), exam_form(exam, **{"exam_clips-0-DELETE": "on"}), follow=True)

        assert response.status_code == 200
        assert not ExamHazardClip.objects.filter(pk=removed.pk).exists()
        assert exam.exam_clips.count() == 1

    def test_removing_a_question_leaves_the_question_bank_alone(self, editor_client, make_question, make_clip):
        from apps.learning.models import Question

        exam = exam_with_two_of_each(make_question, make_clip)
        asked = exam.exam_questions.order_by("order")[0].question_id

        editor_client.post(exam_url(exam), exam_form(exam, **{"exam_questions-0-DELETE": "on"}), follow=True)

        assert Question.objects.filter(pk=asked).exists()

    def test_a_question_can_still_be_added(self, editor_client, make_question, make_clip):
        exam = exam_with_two_of_each(make_question, make_clip)
        extra = make_question(key="mirrors")

        editor_client.post(
            exam_url(exam),
            exam_form(
                exam,
                **{
                    "exam_questions-TOTAL_FORMS": "3",
                    "exam_questions-2-question": extra.pk,
                    "exam_questions-2-order": "3",
                },
            ),
            follow=True,
        )

        assert exam.exam_questions.count() == 3

    def test_one_save_can_both_add_and_remove(self, editor_client, make_question, make_clip):
        exam = exam_with_two_of_each(make_question, make_clip)
        extra = make_question(key="mirrors")

        editor_client.post(
            exam_url(exam),
            exam_form(
                exam,
                **{
                    "exam_questions-0-DELETE": "on",
                    "exam_questions-TOTAL_FORMS": "3",
                    "exam_questions-2-question": extra.pk,
                    "exam_questions-2-order": "3",
                },
            ),
            follow=True,
        )

        assert [place.question.key for place in exam.exam_questions.order_by("order")] == ["stopping", "mirrors"]

    def test_the_last_question_and_clip_cannot_both_go(self, editor_client, make_question, make_clip):
        exam = PracticeExam.objects.create(slug="practice-2", title="Practice 2", kind=ExamKind.PRACTICE, pass_mark=1)
        exam.exam_questions.create(question=make_question(key="distraction"), order=1)

        response = editor_client.post(exam_url(exam), exam_form(exam, **{"exam_questions-0-DELETE": "on"}))

        assert response.status_code == 200
        assert "needs something to sit" in response.content.decode()
        assert exam.exam_questions.count() == 1


class TestTheMediaAssetPage:
    def test_a_saved_track_offers_the_delete_box(self, editor_client):
        asset = video()
        track(asset, SubtitleLanguage.EN_GB)

        page = editor_client.get(asset_url(asset)).content.decode()

        assert "subtitles-0-DELETE" in page

    def test_an_editor_removes_a_track(self, editor_client):
        asset = video()
        removed = track(asset, SubtitleLanguage.EN_GB)

        response = editor_client.post(asset_url(asset), asset_form(asset, **{"subtitles-0-DELETE": "on"}), follow=True)

        assert response.status_code == 200
        assert not Subtitle.objects.filter(pk=removed.pk).exists()

    def test_removing_one_track_leaves_the_others(self, editor_client):
        from apps.learning.tests.test_subtitles import vtt

        asset = video()
        track(asset, SubtitleLanguage.EN_GB)
        track(asset, SubtitleLanguage.FR_FR, file=vtt("fr.vtt"))

        editor_client.post(asset_url(asset), asset_form(asset, **{"subtitles-0-DELETE": "on"}), follow=True)

        assert [kept.language for kept in asset.subtitles.all()] == [SubtitleLanguage.FR_FR]

    def test_the_asset_itself_survives(self, editor_client):
        asset = video()
        track(asset, SubtitleLanguage.EN_GB)

        editor_client.post(asset_url(asset), asset_form(asset, **{"subtitles-0-DELETE": "on"}), follow=True)

        asset.refresh_from_db()
        assert asset.title == "A clip"


class TestWithoutThePermission:
    def test_the_delete_box_is_not_offered(self, no_delete_client, make_question, make_clip):
        exam = exam_with_two_of_each(make_question, make_clip)

        page = no_delete_client.get(exam_url(exam)).content.decode()

        assert "exam_questions-0-DELETE" not in page
        assert "exam_clips-0-DELETE" not in page

    def test_a_hand_written_delete_is_ignored(self, no_delete_client, make_question, make_clip):
        exam = exam_with_two_of_each(make_question, make_clip)

        no_delete_client.post(exam_url(exam), exam_form(exam, **{"exam_questions-0-DELETE": "on"}), follow=True)

        assert exam.exam_questions.count() == 2

    def test_a_subtitle_track_cannot_be_removed_either(self, no_delete_client):
        asset = video()
        kept = track(asset, SubtitleLanguage.EN_GB)

        no_delete_client.post(asset_url(asset), asset_form(asset, **{"subtitles-0-DELETE": "on"}), follow=True)

        assert Subtitle.objects.filter(pk=kept.pk).exists()

    def test_an_outsider_cannot_reach_the_page_at_all(self, client, outsider, make_question, make_clip):
        exam = exam_with_two_of_each(make_question, make_clip)
        client.force_login(outsider)

        assert client.get(exam_url(exam)).status_code == 403
