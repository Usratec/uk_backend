from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.notifications.models import Notification, NotificationKind
from apps.notifications.services import LIST_LIMIT
from apps.notifications.tests.conftest import make_notification

pytestmark = pytest.mark.django_db

LIST_URL = reverse("notification-list")
READ_ALL_URL = reverse("notification-read-all")


def read_url(notification_id) -> str:
    return reverse("notification-read", args=[notification_id])


def aged(notification: Notification, minutes: int) -> Notification:
    Notification.objects.filter(pk=notification.pk).update(created_at=timezone.now() - timedelta(minutes=minutes))
    notification.refresh_from_db()
    return notification


class TestList:
    def test_returns_the_fields_the_frontend_reads(self, signed_in, learner):
        make_notification(learner, link="/my-package")

        response = signed_in.get(LIST_URL)

        assert response.status_code == 200
        assert set(response.data) == {"unread_count", "results"}
        assert set(response.data["results"][0]) == {
            "id",
            "kind",
            "title",
            "text",
            "link",
            "created_at",
            "is_read",
        }

    def test_newest_first(self, signed_in, learner):
        oldest = aged(make_notification(learner, title="Oldest"), minutes=30)
        middle = aged(make_notification(learner, title="Middle"), minutes=15)
        newest = make_notification(learner, title="Newest")

        ids = [row["id"] for row in signed_in.get(LIST_URL).data["results"]]

        assert ids == [newest.pk, middle.pk, oldest.pk]

    def test_notifications_created_together_still_come_back_in_a_fixed_order(self, signed_in, learner):
        first = make_notification(learner, title="First")
        second = make_notification(learner, title="Second")
        Notification.objects.update(created_at=timezone.now())

        ids = [row["id"] for row in signed_in.get(LIST_URL).data["results"]]

        assert ids == [second.pk, first.pk]

    def test_counts_only_the_unread(self, signed_in, learner):
        make_notification(learner)
        make_notification(learner)
        make_notification(learner, is_read=True)

        assert signed_in.get(LIST_URL).data["unread_count"] == 2

    def test_the_count_covers_everything_not_just_the_slice_served(self, signed_in, learner):
        Notification.objects.bulk_create(
            Notification(user=learner, kind=NotificationKind.PACKAGE_ACTIVATED, title=f"#{index}", text="x")
            for index in range(LIST_LIMIT + 5)
        )

        response = signed_in.get(LIST_URL)

        assert len(response.data["results"]) == LIST_LIMIT
        assert response.data["unread_count"] == LIST_LIMIT + 5

    def test_an_absent_link_is_null(self, signed_in, learner):
        make_notification(learner)

        assert signed_in.get(LIST_URL).data["results"][0]["link"] is None

    def test_nothing_yet_is_an_empty_list_and_a_zero_count(self, signed_in):
        response = signed_in.get(LIST_URL)

        assert response.data == {"unread_count": 0, "results": []}

    def test_another_learners_notifications_are_not_listed(self, signed_in, learner, other_learner):
        mine = make_notification(learner, title="Mine")
        make_notification(other_learner, title="Theirs")

        results = signed_in.get(LIST_URL).data["results"]

        assert [row["id"] for row in results] == [mine.pk]

    def test_signing_in_is_required(self, api):
        response = api.get(LIST_URL)

        assert response.status_code == 401
        assert "detail" in response.json()

    def test_a_forged_token_is_rejected(self, api, learner):
        make_notification(learner)
        api.credentials(HTTP_AUTHORIZATION="Bearer expired.or.forged")

        assert api.get(LIST_URL).status_code == 401

    def test_a_closed_account_cannot_read_its_notifications(self, api, learner):
        from rest_framework_simplejwt.tokens import RefreshToken

        make_notification(learner)
        token = RefreshToken.for_user(learner).access_token
        learner.is_active = False
        learner.save(update_fields=["is_active"])
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

        assert api.get(LIST_URL).status_code == 401

    def test_one_query_each_for_the_rows_and_the_count(self, signed_in, learner, django_assert_num_queries):
        for _ in range(5):
            make_notification(learner)

        with django_assert_num_queries(2):
            signed_in.get(LIST_URL)


class TestMarkOneRead:
    def test_marks_it_read(self, signed_in, learner):
        notification = make_notification(learner)

        response = signed_in.post(read_url(notification.pk))

        assert response.status_code == 200
        notification.refresh_from_db()
        assert notification.is_read is True

    def test_repeating_the_request_is_safe(self, signed_in, learner):
        notification = make_notification(learner)

        first = signed_in.post(read_url(notification.pk))
        second = signed_in.post(read_url(notification.pk))

        assert [first.status_code, second.status_code] == [200, 200]
        notification.refresh_from_db()
        assert notification.is_read is True

    def test_a_missing_id_is_a_404_with_a_detail_key(self, signed_in):
        response = signed_in.post(read_url(999999))

        assert response.status_code == 404
        assert "detail" in response.json()

    def test_another_learners_notification_is_a_404_and_is_left_alone(self, signed_in, other_learner):
        theirs = make_notification(other_learner)

        response = signed_in.post(read_url(theirs.pk))

        assert response.status_code == 404
        theirs.refresh_from_db()
        assert theirs.is_read is False

    def test_the_request_body_cannot_change_anything_else(self, signed_in, learner):
        notification = make_notification(learner, title="Package activated")

        signed_in.post(
            read_url(notification.pk),
            {"title": "Rewritten", "text": "Rewritten", "kind": NotificationKind.STREAK_MILESTONE, "user": 999},
            format="json",
        )

        notification.refresh_from_db()
        assert notification.title == "Package activated"
        assert notification.kind == NotificationKind.PACKAGE_ACTIVATED
        assert notification.user_id == learner.pk

    def test_signing_in_is_required(self, api, learner):
        notification = make_notification(learner)

        assert api.post(read_url(notification.pk)).status_code == 401
        notification.refresh_from_db()
        assert notification.is_read is False

    @pytest.mark.parametrize("method", ["get", "put", "patch", "delete"])
    def test_only_post_is_allowed(self, signed_in, learner, method):
        notification = make_notification(learner)

        assert getattr(signed_in, method)(read_url(notification.pk)).status_code == 405


class TestMarkAllRead:
    def test_marks_every_unread_notification_of_this_learners(self, signed_in, learner):
        make_notification(learner)
        make_notification(learner)

        response = signed_in.post(READ_ALL_URL)

        assert response.status_code == 200
        assert response.data == {"updated": 2}
        assert Notification.objects.filter(user=learner, is_read=False).count() == 0

    def test_counts_only_what_it_changed(self, signed_in, learner):
        make_notification(learner, is_read=True)
        make_notification(learner)

        assert signed_in.post(READ_ALL_URL).data == {"updated": 1}

    def test_repeating_the_request_is_safe(self, signed_in, learner):
        make_notification(learner)

        assert signed_in.post(READ_ALL_URL).data == {"updated": 1}
        assert signed_in.post(READ_ALL_URL).data == {"updated": 0}

    def test_nothing_to_do_is_not_an_error(self, signed_in):
        assert signed_in.post(READ_ALL_URL).status_code == 200

    def test_another_learners_notifications_are_untouched(self, signed_in, learner, other_learner):
        make_notification(learner)
        theirs = make_notification(other_learner)

        assert signed_in.post(READ_ALL_URL).data == {"updated": 1}
        theirs.refresh_from_db()
        assert theirs.is_read is False

    def test_it_is_one_update_however_many_rows(self, signed_in, learner, django_assert_num_queries):
        for _ in range(10):
            make_notification(learner)

        with django_assert_num_queries(1):
            signed_in.post(READ_ALL_URL)

    def test_signing_in_is_required(self, api, learner):
        make_notification(learner)

        assert api.post(READ_ALL_URL).status_code == 401
        assert Notification.objects.filter(is_read=False).count() == 1

    def test_read_all_is_not_read_as_a_notification_id(self, signed_in):
        assert signed_in.post(READ_ALL_URL).status_code == 200
