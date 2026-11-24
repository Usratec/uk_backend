from unittest.mock import patch
from uuid import uuid4

import pytest
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.db import OperationalError, connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.core.models import PublishStatus
from apps.entitlements import services as entitlements
from apps.entitlements.models import RevocationReason, Subscription
from apps.integrations import cloudfront
from apps.learning import access_cache, key_auth, media_processing
from apps.learning.models import ContentType, LearningContent, MediaAsset, VideoProcessingStatus
from apps.learning.tests.conftest import sign_in
from apps.learning.tests.test_video_playback import KEY, key_url, playback_url, ready_video

pytestmark = pytest.mark.django_db


@pytest.fixture
def distribution(settings, signing_key_pem):
    settings.CLOUDFRONT_DOMAIN = "media.1theory.test"
    settings.CLOUDFRONT_KEY_PAIR_ID = "K1EXAMPLE"
    settings.CLOUDFRONT_PRIVATE_KEY = signing_key_pem
    settings.CLOUDFRONT_COOKIE_DOMAIN = ".1theory.test"
    cloudfront._load_private_key.cache_clear()
    return settings


def publish_video(subchapter, asset: MediaAsset, slug: str = "braking-distances") -> LearningContent:
    return LearningContent.objects.create(
        subchapter=subchapter,
        slug=slug,
        title=slug.replace("-", " ").title(),
        content_type=ContentType.VIDEO,
        media=asset,
        order=2,
        status=PublishStatus.PUBLISHED,
    )


@pytest.fixture
def lesson(published_tree):
    _, subchapter, _ = published_tree
    asset = ready_video()
    publish_video(subchapter, asset)
    return subchapter, asset


@pytest.fixture
def encrypted(lesson):
    _, asset = lesson
    MediaAsset.objects.filter(pk=asset.pk).update(hls_key=KEY)
    asset.refresh_from_db()
    return asset


def queries_during(call) -> int:
    with CaptureQueriesContext(connection) as captured:
        call()
    return len(captured)


class TestTheAnswerIsOnlyWorkedOutOnce:
    def test_a_second_ask_runs_no_query_at_all(self, lesson, student):
        _, asset = lesson
        learner = student()

        assert media_processing.may_watch(student=learner, asset=asset) is True
        assert queries_during(lambda: media_processing.may_watch(student=learner, asset=asset)) == 0

    def test_the_check_itself_runs_once_for_many_asks(self, lesson, student):
        _, asset = lesson
        learner = student()

        with patch.object(media_processing, "_may_watch", wraps=media_processing._may_watch) as checked:
            for _ in range(5):
                media_processing.may_watch(student=learner, asset=asset)

        assert checked.call_count == 1

    def test_a_denial_is_remembered_too(self, lesson, student):
        _, asset = lesson
        nobody = User.objects.create_user(email="nobody@example.com", first_name="Nobody")

        with patch.object(media_processing, "_may_watch", wraps=media_processing._may_watch) as checked:
            first = media_processing.may_watch(student=nobody, asset=asset)
            second = media_processing.may_watch(student=nobody, asset=asset)

        assert (first, second) == (False, False)
        assert checked.call_count == 1

    def test_the_remembered_value_is_a_plain_yes_or_no_and_nothing_about_the_student(self, lesson, student):
        _, asset = lesson
        learner = student()
        media_processing.may_watch(student=learner, asset=asset)

        key = access_cache.decision_key(user_id=learner.pk, asset_id=asset.pk, generation=0)

        assert cache.get(key) is True


class TestNobodyGetsSomebodyElseSAnswer:
    def test_two_students_never_share_an_entry(self, lesson, student, packages):
        subchapter, asset = lesson
        allowed = student()
        refused = User.objects.create_user(email="refused@example.com", first_name="Nope")

        assert media_processing.may_watch(student=allowed, asset=asset) is True
        assert media_processing.may_watch(student=refused, asset=asset) is False

    def test_a_remembered_yes_does_not_carry_to_another_video(self, lesson, student):
        subchapter, asset = lesson
        other = ready_video("Something they never bought")
        learner = student()

        assert media_processing.may_watch(student=learner, asset=asset) is True
        assert media_processing.may_watch(student=learner, asset=other) is False

    def test_the_key_names_both_the_student_and_the_video(self, lesson, student):
        _, asset = lesson
        learner = student()

        key = access_cache.decision_key(user_id=learner.pk, asset_id=asset.pk, generation=0)

        assert str(learner.pk) in key
        assert str(asset.pk) in key
        assert key != access_cache.decision_key(user_id=learner.pk, asset_id=asset.pk + 1, generation=0)
        assert key != access_cache.decision_key(user_id=learner.pk + 1, asset_id=asset.pk, generation=0)

    def test_an_anonymous_visitor_is_never_remembered(self, lesson):
        _, asset = lesson
        visitor = AnonymousUser()

        assert media_processing.may_watch(student=visitor, asset=asset) is False
        # Not under a key naming the student they do not have, and not under one naming none.
        assert cache.get(f"{access_cache.NAMESPACE}:None:{asset.pk}:0") is None
        assert cache.get(f"{access_cache.NAMESPACE}:generation:None") is None

    def test_an_anonymous_visitor_cannot_land_on_a_student_s_entry(self, lesson, student):
        _, asset = lesson
        learner = student()
        media_processing.may_watch(student=learner, asset=asset)

        with patch.object(media_processing, "_may_watch", return_value=False) as checked:
            assert media_processing.may_watch(student=AnonymousUser(), asset=asset) is False

        assert checked.call_count == 1


class TestTheAnswerDoesNotOutliveTheAccess:
    def test_it_is_kept_for_a_minute(self):
        assert access_cache.TTL_SECONDS == 60

    def test_once_it_has_gone_the_check_runs_again(self, lesson, student):
        _, asset = lesson
        learner = student()
        media_processing.may_watch(student=learner, asset=asset)

        cache.delete(access_cache.decision_key(user_id=learner.pk, asset_id=asset.pk, generation=0))

        with patch.object(media_processing, "_may_watch", wraps=media_processing._may_watch) as checked:
            media_processing.may_watch(student=learner, asset=asset)

        assert checked.call_count == 1

    def test_an_expired_subscription_is_refused_once_the_answer_has_gone(self, lesson, student):
        _, asset = lesson
        learner = student()
        assert media_processing.may_watch(student=learner, asset=asset) is True

        Subscription.objects.filter(user=learner).update(package_expires_at="2020-01-01T00:00:00Z")
        cache.clear()

        assert media_processing.may_watch(student=learner, asset=asset) is False

    def test_a_refund_refuses_the_video_straight_away(self, lesson, student, django_capture_on_commit_callbacks):
        _, asset = lesson
        learner = student()
        assert media_processing.may_watch(student=learner, asset=asset) is True

        order = Subscription.objects.get(user=learner).order
        with django_capture_on_commit_callbacks(execute=True):
            entitlements.revoke(order, RevocationReason.REFUND)

        assert media_processing.may_watch(student=learner, asset=asset) is False

    def test_a_purchase_is_recognised_straight_away(self, lesson, packages, django_capture_on_commit_callbacks):
        _, asset = lesson
        buyer = User.objects.create_user(email="buyer@example.com", first_name="Bee")
        assert media_processing.may_watch(student=buyer, asset=asset) is False

        order = Order.objects.create(
            package=packages["starter"],
            email=buyer.email,
            user=buyer,
            status=OrderStatus.PAID,
            paid_at=timezone.now(),
            original_price=packages["starter"].price,
            final_price=packages["starter"].price,
        )
        with django_capture_on_commit_callbacks(execute=True):
            entitlements.activate(order, buyer)

        assert media_processing.may_watch(student=buyer, asset=asset) is True

    def test_forgetting_one_student_leaves_the_others_alone(self, lesson, student):
        _, asset = lesson
        keeper = student("starter")
        loser = student("premium")
        media_processing.may_watch(student=keeper, asset=asset)
        media_processing.may_watch(student=loser, asset=asset)

        access_cache.forget(loser.pk)

        assert cache.get(access_cache.decision_key(user_id=keeper.pk, asset_id=asset.pk, generation=0)) is True
        assert cache.get(access_cache.decision_key(user_id=loser.pk, asset_id=asset.pk, generation=0)) is True
        assert access_cache._generation_of(loser.pk) == 1
        assert media_processing.may_watch(student=loser, asset=asset) is True

    def test_forgetting_a_student_nothing_is_known_about_does_not_fail(self, student):
        learner = student()

        access_cache.forget(learner.pk)

        assert access_cache._generation_of(learner.pk) == 1

    def test_withdrawing_a_lesson_is_refused_once_the_answer_has_gone(self, lesson, student):
        subchapter, asset = lesson
        learner = student()
        assert media_processing.may_watch(student=learner, asset=asset) is True

        LearningContent.objects.filter(media=asset).update(status=PublishStatus.DRAFT)
        cache.clear()

        assert media_processing.may_watch(student=learner, asset=asset) is False


class TestWhenTheCacheOrTheDatabaseMisbehaves:
    def test_a_cache_that_cannot_be_read_falls_back_to_the_real_check(self, lesson, student):
        _, asset = lesson
        learner = student()

        with patch.object(cache, "get_or_set", side_effect=ConnectionError("redis is down")):
            assert media_processing.may_watch(student=learner, asset=asset) is True

    def test_a_cache_that_cannot_be_written_still_answers(self, lesson, student):
        _, asset = lesson
        learner = student()

        with patch.object(cache, "set", side_effect=ConnectionError("redis is down")):
            assert media_processing.may_watch(student=learner, asset=asset) is True

        assert media_processing.may_watch(student=learner, asset=asset) is True

    def test_a_cache_that_cannot_be_cleared_does_not_break_a_refund(self, lesson, student):
        _, asset = lesson
        learner = student()

        with patch.object(cache, "incr", side_effect=ConnectionError("redis is down")):
            access_cache.forget(learner.pk)

    def test_a_database_failure_is_never_remembered_as_a_denial(self, lesson, student):
        _, asset = lesson
        learner = student()

        with patch.object(media_processing, "_may_watch", side_effect=OperationalError("the database went away")):
            with pytest.raises(OperationalError):
                media_processing.may_watch(student=learner, asset=asset)

        assert cache.get(access_cache.decision_key(user_id=learner.pk, asset_id=asset.pk, generation=0)) is None
        assert media_processing.may_watch(student=learner, asset=asset) is True

    def test_a_mangled_entry_is_treated_as_nothing_remembered(self, lesson, student):
        _, asset = lesson
        learner = student()
        key = access_cache.decision_key(user_id=learner.pk, asset_id=asset.pk, generation=0)
        cache.set(key, "yes please", 60)

        with patch.object(media_processing, "_may_watch", return_value=False) as checked:
            assert media_processing.may_watch(student=learner, asset=asset) is False

        assert checked.call_count == 1

    def test_two_requests_arriving_together_reach_the_same_answer(self, lesson, student):
        _, asset = lesson
        learner = student()

        # Nothing is remembered yet, so both do the work; neither may invent a different answer.
        first = media_processing.may_watch(student=learner, asset=asset)
        cache.delete(access_cache.decision_key(user_id=learner.pk, asset_id=asset.pk, generation=0))
        second = media_processing.may_watch(student=learner, asset=asset)

        assert first is second is True

    def test_an_answer_remembered_during_a_change_cannot_be_resurrected_by_a_later_one(self, lesson, student):
        _, asset = lesson
        learner = student()
        media_processing.may_watch(student=learner, asset=asset)

        access_cache.forget(learner.pk)
        media_processing.may_watch(student=learner, asset=asset)
        access_cache.forget(learner.pk)

        generation = access_cache._generation_of(learner.pk)
        assert generation == 2
        assert cache.get(access_cache.decision_key(user_id=learner.pk, asset_id=asset.pk, generation=2)) is None


class TestBothEndpointsAskTheSameQuestion:
    def test_the_key_endpoint_reuses_what_playback_worked_out(self, api, student, encrypted, distribution):
        learner = sign_in(api, student())

        assert learner.get(playback_url(encrypted.upload_token)).status_code == 200
        with patch.object(media_processing, "_may_watch", wraps=media_processing._may_watch) as checked:
            response = learner.get(key_url(encrypted.upload_token))

        assert response.status_code == 200
        assert response.content == KEY
        assert checked.call_count == 0

    def test_the_key_endpoint_still_checks_for_itself_when_nothing_is_remembered(self, api, student, encrypted):
        learner = sign_in(api, student())

        with patch.object(media_processing, "_may_watch", wraps=media_processing._may_watch) as checked:
            response = learner.get(key_url(encrypted.upload_token))

        assert response.status_code == 200
        assert checked.call_count == 1

    def test_the_key_endpoint_still_needs_authentication(self, api, encrypted):
        response = api.get(key_url(encrypted.upload_token))

        assert response.status_code == 401
        assert KEY not in response.content

    def test_a_remembered_yes_is_no_substitute_for_signing_in(self, api, student, encrypted, distribution):
        learner = sign_in(api, student())
        learner.get(playback_url(encrypted.upload_token))

        response = APIClient().get(key_url(encrypted.upload_token))

        assert response.status_code == 401
        assert KEY not in response.content

    def test_the_safari_cookie_is_still_verified(self, api, student, encrypted, distribution):
        learner = sign_in(api, student())
        learner.get(playback_url(encrypted.upload_token))

        forged = APIClient()
        forged.cookies[key_auth.COOKIE_NAME] = "not-a-real-signature"
        response = forged.get(key_url(encrypted.upload_token))

        assert response.status_code == 401
        assert KEY not in response.content

    def test_a_refund_between_playback_and_the_key_refuses_the_key(
        self, api, student, encrypted, distribution, django_capture_on_commit_callbacks
    ):
        learner = student()
        signed_in = sign_in(api, learner)
        assert signed_in.get(playback_url(encrypted.upload_token)).status_code == 200

        order = Subscription.objects.get(user=learner).order
        with django_capture_on_commit_callbacks(execute=True):
            entitlements.revoke(order, RevocationReason.REFUND)

        response = signed_in.get(key_url(encrypted.upload_token))

        assert response.status_code == 403
        assert KEY not in response.content

    def test_a_student_who_may_not_watch_is_refused_by_both(self, api, encrypted, distribution):
        outsider = User.objects.create_user(email="outsider2@example.com", first_name="Out")
        signed_in = sign_in(api, outsider)

        assert signed_in.get(playback_url(encrypted.upload_token)).status_code == 402
        assert signed_in.get(key_url(encrypted.upload_token)).status_code == 403


class TestTheEndpointsRunFewerQueries:
    def test_playback_only_looks_up_the_video_when_the_answer_is_known(self, api, student, lesson, distribution):
        _, asset = lesson
        learner = sign_in(api, student())
        learner.get(playback_url(asset.upload_token))

        assert queries_during(lambda: learner.get(playback_url(asset.upload_token))) == 1

    def test_the_key_endpoint_only_looks_up_the_video_when_the_answer_is_known(self, api, student, encrypted):
        learner = sign_in(api, student())
        learner.get(key_url(encrypted.upload_token))

        assert queries_during(lambda: learner.get(key_url(encrypted.upload_token))) == 1


class TestNothingAboutLegacyPlaybackChanged:
    def test_a_legacy_video_still_plays_from_its_source(self, api, student, published_tree, bucket):
        _, subchapter, _ = published_tree
        asset = MediaAsset.objects.create(
            title="An old clip",
            kind="video",
            file="learning/video/old.mp4",
            content_type="video/mp4",
            size_bytes=2048,
            upload_token=uuid4(),
            processing_status=VideoProcessingStatus.LEGACY,
        )
        publish_video(subchapter, asset, slug="an-old-clip")
        learner = sign_in(api, student())

        response = learner.get(playback_url(asset.upload_token))

        assert response.status_code == 200
        body = response.json()
        assert body["playbackType"] == "progressive"
        assert body["mp4Url"] == ""
        assert body["withCredentials"] is False

    def test_the_playback_response_still_carries_every_field_the_player_reads(
        self, api, student, lesson, distribution
    ):
        _, asset = lesson
        learner = sign_in(api, student())

        body = learner.get(playback_url(asset.upload_token)).json()

        assert sorted(body) == [
            "expiresAt",
            "mediaId",
            "mp4Url",
            "playbackType",
            "qualities",
            "url",
            "withCredentials",
        ]

    def test_an_unready_video_still_answers_not_ready_rather_than_refused(self, api, student, lesson, distribution):
        _, asset = lesson
        MediaAsset.objects.filter(pk=asset.pk).update(
            processing_status=VideoProcessingStatus.PENDING_UPLOAD, hls_master_key=""
        )
        learner = sign_in(api, student())

        assert learner.get(playback_url(asset.upload_token)).status_code == 409

    def test_the_playback_url_is_still_where_it_was(self, lesson):
        _, asset = lesson

        assert reverse("learn-media-playback", args=[asset.upload_token]) == (
            f"/api/learn/media/{asset.upload_token}/playback/"
        )
