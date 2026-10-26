import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.core.models import PublishStatus
from apps.learning.models import ContentType, LearningContent, MediaAsset, MediaKind, Subchapter, VideoProgress
from apps.learning.tests.conftest import sign_in

pytestmark = pytest.mark.django_db

TOPICS_URL = reverse("learn-topics")


def progress_url(slug: str) -> str:
    return reverse("learn-video-progress", args=[slug])


def lesson_url(slug: str) -> str:
    return reverse("learn-lesson", args=[slug])


def make_video_asset(name: str = "clip.mp4") -> MediaAsset:
    return MediaAsset.objects.create(
        title="A clip",
        kind=MediaKind.VIDEO,
        file=SimpleUploadedFile(name, b"not really a video", content_type="video/mp4"),
    )


@pytest.fixture
def live_video(published_tree):
    _, subchapter, _ = published_tree
    return LearningContent.objects.create(
        subchapter=subchapter,
        slug="braking-distances",
        title="Braking distances",
        content_type=ContentType.VIDEO,
        media=make_video_asset(),
        order=2,
        status=PublishStatus.PUBLISHED,
    )


@pytest.fixture
def learner(api, student):
    return sign_in(api, student())


def blocks_of(response) -> list[dict]:
    return response.json()["lesson"]["blocks"]


def video_block(response, slug_title: str = "Braking distances") -> dict:
    return next(block for block in blocks_of(response) if block.get("title") == slug_title)


def lesson_card(response, slug: str) -> dict:
    lessons = [lesson for topic in response.json()["topics"] for lesson in topic["lessons"]]
    return next(lesson for lesson in lessons if lesson["slug"] == slug)


class TestSaving:
    def test_a_learner_can_save_where_they_are_up_to(self, learner, live_video):
        response = learner.post(
            progress_url(live_video.slug), {"positionSeconds": 205, "durationSeconds": 620}, format="json"
        )

        assert response.status_code == 200
        assert response.json() == {
            "slug": "braking-distances",
            "positionSeconds": 205.0,
            "durationSeconds": 620.0,
            "resumeAt": "03:25",
            "hasProgress": True,
            "finished": False,
            "updatedAt": response.json()["updatedAt"],
        }

    def test_the_snake_case_keys_the_frontend_may_send_are_accepted(self, learner, live_video):
        response = learner.post(
            progress_url(live_video.slug), {"position_seconds": 205, "duration_seconds": 620}, format="json"
        )

        assert response.status_code == 200
        assert response.json()["positionSeconds"] == 205.0
        assert response.json()["durationSeconds"] == 620.0

    def test_saving_again_updates_the_one_row_rather_than_adding_another(self, learner, live_video):
        for position in (10, 60, 125):
            learner.post(
                progress_url(live_video.slug), {"positionSeconds": position, "durationSeconds": 620}, format="json"
            )

        assert VideoProgress.objects.filter(content=live_video).count() == 1
        saved = VideoProgress.objects.get(content=live_video)
        assert float(saved.position_seconds) == 125.0

    def test_a_duration_saved_earlier_is_kept_when_a_later_request_omits_it(self, learner, live_video):
        learner.post(progress_url(live_video.slug), {"positionSeconds": 10, "durationSeconds": 620}, format="json")

        response = learner.post(progress_url(live_video.slug), {"positionSeconds": 70}, format="json")

        assert response.json()["durationSeconds"] == 620.0

    def test_a_position_past_the_end_is_clamped_to_the_duration(self, learner, live_video):
        response = learner.post(
            progress_url(live_video.slug), {"positionSeconds": 9000, "durationSeconds": 620}, format="json"
        )

        assert response.json()["positionSeconds"] == 620.0
        assert response.json()["finished"] is True

    def test_reaching_the_end_does_not_mark_the_lesson_complete(self, learner, live_video):
        learner.post(progress_url(live_video.slug), {"positionSeconds": 620, "durationSeconds": 620}, format="json")

        assert learner.get(lesson_url(live_video.subchapter.slug)).json()["done"] is False

    def test_video_progress_accepts_form_data(self, learner, live_video):
        response = learner.post(progress_url(live_video.slug), {"position_seconds": 205, "duration_seconds": 620})

        assert response.status_code == 200
        assert response.json()["positionSeconds"] == 205.0
        assert response.json()["durationSeconds"] == 620.0

    def test_the_start_of_a_very_short_video_is_not_called_finished(self, learner, live_video):
        response = learner.post(
            progress_url(live_video.slug), {"positionSeconds": 0, "durationSeconds": 1}, format="json"
        )

        assert response.json()["finished"] is False


class TestWhatIsRefused:
    def test_a_lesson_that_is_not_a_video_answers_404(self, learner, published_tree):
        _, _, theory = published_tree

        response = learner.post(progress_url(theory.slug), {"positionSeconds": 10}, format="json")

        assert response.status_code == 404
        assert not VideoProgress.objects.exists()

    def test_a_video_in_an_unpublished_lesson_answers_404(self, learner, live_video):
        live_video.subchapter.status = PublishStatus.DRAFT
        live_video.subchapter.save(update_fields=["status"])

        response = learner.post(progress_url(live_video.slug), {"positionSeconds": 10}, format="json")

        assert response.status_code == 404

    def test_a_negative_position_is_refused(self, learner, live_video):
        response = learner.post(progress_url(live_video.slug), {"positionSeconds": -5}, format="json")

        assert response.status_code == 400
        assert not VideoProgress.objects.exists()

    def test_a_request_without_a_position_is_refused_rather_than_read_as_zero(self, learner, live_video):
        learner.post(progress_url(live_video.slug), {"positionSeconds": 205, "durationSeconds": 620}, format="json")

        response = learner.post(progress_url(live_video.slug), {"durationSeconds": 620}, format="json")

        assert response.status_code == 400
        assert float(VideoProgress.objects.get(content=live_video).position_seconds) == 205.0


class TestReadingItBack:
    def test_the_lesson_detail_carries_the_saved_position(self, learner, live_video):
        learner.post(progress_url(live_video.slug), {"positionSeconds": 205, "durationSeconds": 620}, format="json")

        block = video_block(learner.get(lesson_url(live_video.subchapter.slug)))

        assert block["progress"]["positionSeconds"] == 205.0
        assert block["progress"]["durationSeconds"] == 620.0
        assert block["progress"]["resumeAt"] == "03:25"
        assert block["progress"]["hasProgress"] is True

    def test_a_video_with_nothing_saved_reads_as_the_beginning(self, learner, live_video):
        block = video_block(learner.get(lesson_url(live_video.subchapter.slug)))

        assert block["progress"] == {
            "positionSeconds": 0.0,
            "durationSeconds": None,
            "resumeAt": "00:00",
            "hasProgress": False,
            "finished": False,
            "updatedAt": None,
        }

    def test_the_topic_list_offers_the_video_to_resume(self, learner, live_video):
        learner.post(progress_url(live_video.slug), {"positionSeconds": 205, "durationSeconds": 620}, format="json")

        card = lesson_card(learner.get(TOPICS_URL), live_video.subchapter.slug)

        assert card["videoProgress"]["slug"] == "braking-distances"
        assert card["videoProgress"]["positionSeconds"] == 205.0
        assert card["videoProgress"]["resumeAt"] == "03:25"

    def test_a_lesson_with_nothing_saved_offers_nothing_to_resume(self, learner, live_video):
        card = lesson_card(learner.get(TOPICS_URL), live_video.subchapter.slug)

        assert card["videoProgress"] is None

    def test_the_topic_list_offers_the_video_watched_most_recently(self, learner, live_video):
        second = LearningContent.objects.create(
            subchapter=live_video.subchapter,
            slug="stopping-distances",
            title="Stopping distances",
            content_type=ContentType.VIDEO,
            media=make_video_asset("second.mp4"),
            order=3,
            status=PublishStatus.PUBLISHED,
        )
        learner.post(progress_url(live_video.slug), {"positionSeconds": 10}, format="json")
        learner.post(progress_url(second.slug), {"positionSeconds": 40}, format="json")

        card = lesson_card(learner.get(TOPICS_URL), live_video.subchapter.slug)

        assert card["videoProgress"]["slug"] == "stopping-distances"

    def test_the_other_blocks_in_a_lesson_are_unchanged(self, learner, live_video, published_tree):
        _, _, theory = published_tree

        theory_block = next(block for block in blocks_of(learner.get(lesson_url(live_video.subchapter.slug))))

        assert theory_block["type"] == "html"
        assert "progress" not in theory_block


class TestOneLearnerCannotSeeAnother:
    def test_two_learners_hold_their_own_positions_in_the_same_video(self, api, student, live_video):
        first = sign_in(api, student("starter"))
        first.post(progress_url(live_video.slug), {"positionSeconds": 205, "durationSeconds": 620}, format="json")

        from rest_framework.test import APIClient

        second = sign_in(APIClient(), student("premium"))
        second.post(progress_url(live_video.slug), {"positionSeconds": 42, "durationSeconds": 620}, format="json")

        assert VideoProgress.objects.filter(content=live_video).count() == 2
        assert video_block(first.get(lesson_url(live_video.subchapter.slug)))["progress"]["positionSeconds"] == 205.0
        assert video_block(second.get(lesson_url(live_video.subchapter.slug)))["progress"]["positionSeconds"] == 42.0

    def test_saving_never_touches_another_learners_row(self, api, student, live_video):
        from rest_framework.test import APIClient

        theirs = student("premium")
        sign_in(APIClient(), theirs).post(
            progress_url(live_video.slug), {"positionSeconds": 205, "durationSeconds": 620}, format="json"
        )

        sign_in(api, student("starter")).post(progress_url(live_video.slug), {"positionSeconds": 1}, format="json")

        assert float(VideoProgress.objects.get(student=theirs).position_seconds) == 205.0


def watched_video(learner, subchapter, index: int) -> LearningContent:
    video = LearningContent.objects.create(
        subchapter=subchapter,
        slug=f"video-{index}",
        title=f"Video {index}",
        content_type=ContentType.VIDEO,
        media=make_video_asset(f"video-{index}.mp4"),
        order=index + 2,
        status=PublishStatus.PUBLISHED,
    )
    learner.post(progress_url(video.slug), {"positionSeconds": 10, "durationSeconds": 620}, format="json")
    return video


def watched_lesson(learner, chapter, index: int) -> None:
    subchapter = Subchapter.objects.create(
        chapter=chapter,
        slug=f"lesson-{index}",
        title=f"Lesson {index}",
        order=index + 2,
        status=PublishStatus.PUBLISHED,
    )
    watched_video(learner, subchapter, index)


def queries_for(client, url) -> int:
    with CaptureQueriesContext(connection) as captured:
        response = client.get(url)
    assert response.status_code == 200, response.status_code
    return len(captured.captured_queries)


class TestItCostsNothingToList:
    def test_the_topic_list_does_not_ask_once_per_lesson_with_a_saved_position(self, learner, published_tree):
        chapter, _, _ = published_tree
        watched_lesson(learner, chapter, 0)
        one = queries_for(learner, TOPICS_URL)

        for index in range(1, 6):
            watched_lesson(learner, chapter, index)
        six = queries_for(learner, TOPICS_URL)

        assert six == one, f"{one} queries for one resumable lesson but {six} for six"

    def test_the_lesson_detail_does_not_ask_once_per_video(self, learner, published_tree):
        _, subchapter, _ = published_tree
        for index in range(5):
            watched_video(learner, subchapter, index)

        with CaptureQueriesContext(connection) as five_videos:
            response = learner.get(lesson_url(subchapter.slug))

        assert response.status_code == 200
        assert len([block for block in blocks_of(response) if block["type"] == "video"]) == 5

        watched_video(learner, subchapter, 5)
        six = queries_for(learner, lesson_url(subchapter.slug))

        assert six == len(five_videos.captured_queries)
