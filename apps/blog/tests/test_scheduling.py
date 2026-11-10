from datetime import timedelta

import pytest
from django.urls import reverse
from django.utils import timezone

from apps.blog import services, tasks
from apps.blog.models import BlogStatus
from apps.core.models import AuditEvent
from apps.staff.authz import PermissionDeniedError

from .conftest import make_post

pytestmark = pytest.mark.django_db

LIST_URL = reverse("blog-post-list")


def tomorrow():
    return timezone.now() + timedelta(days=1)


class TestPublishing:
    def test_a_publisher_publishes_now(self, blog_manager):
        post = make_post(status=BlogStatus.DRAFT)

        services.publish(actor=blog_manager, post=post)
        post.refresh_from_db()

        assert post.status == BlogStatus.PUBLISHED
        assert post.is_live()
        assert post.published_by_id == blog_manager.pk

    def test_publishing_pulls_a_future_date_back_to_now(self, blog_manager):
        post = make_post(status=BlogStatus.SCHEDULED, publish_at=tomorrow())

        services.publish(actor=blog_manager, post=post)

        assert post.is_live()

    def test_staff_without_the_permission_may_not_publish(self, blog_viewer):
        post = make_post(status=BlogStatus.DRAFT)

        with pytest.raises(PermissionDeniedError):
            services.publish(actor=blog_viewer, post=post)

    def test_a_superuser_without_the_grant_may_not_publish(self, root):
        post = make_post(status=BlogStatus.DRAFT)

        with pytest.raises(PermissionDeniedError):
            services.publish(actor=root, post=post)

    def test_an_empty_post_cannot_be_published(self, blog_manager):
        post = make_post(status=BlogStatus.DRAFT, body_html="<p>&nbsp;</p>")

        with pytest.raises(services.PostNotReadyError):
            services.publish(actor=blog_manager, post=post)

    def test_publishing_is_recorded(self, blog_manager):
        post = make_post(status=BlogStatus.DRAFT)

        services.publish(actor=blog_manager, post=post)

        assert AuditEvent.objects.filter(action="blog.post.published").count() == 1

    def test_unpublishing_takes_it_off_the_site_at_once(self, api, blog_manager):
        post = make_post()
        assert api.get(LIST_URL).data["count"] == 1

        services.unpublish(actor=blog_manager, post=post)

        assert api.get(LIST_URL).data["count"] == 0
        assert api.get(reverse("blog-post", args=[post.slug])).status_code == 404

    def test_unpublishing_keeps_what_was_written(self, blog_manager):
        post = make_post()

        services.unpublish(actor=blog_manager, post=post)
        post.refresh_from_db()

        assert post.status == BlogStatus.DRAFT
        assert post.body_html

    def test_staff_without_the_permission_may_not_unpublish(self, blog_viewer):
        post = make_post()

        with pytest.raises(PermissionDeniedError):
            services.unpublish(actor=blog_viewer, post=post)


class TestScheduling:
    def test_a_post_can_be_scheduled_for_a_future_date(self, blog_manager):
        when = tomorrow()
        post = make_post(status=BlogStatus.DRAFT, publish_at=when)

        services.schedule(actor=blog_manager, post=post)
        post.refresh_from_db()

        assert post.status == BlogStatus.SCHEDULED
        assert post.publish_at == when
        assert not post.is_live()

    def test_a_date_in_the_past_is_refused(self, blog_manager):
        post = make_post(status=BlogStatus.DRAFT, publish_at=timezone.now() - timedelta(minutes=1))

        with pytest.raises(services.ScheduleInThePastError):
            services.schedule(actor=blog_manager, post=post)

    def test_no_date_at_all_is_refused(self, blog_manager):
        post = make_post(status=BlogStatus.DRAFT, publish_at=None)

        with pytest.raises(services.ScheduleInThePastError):
            services.schedule(actor=blog_manager, post=post)

    def test_staff_without_the_permission_may_not_schedule(self, blog_viewer):
        post = make_post(status=BlogStatus.DRAFT, publish_at=tomorrow())

        with pytest.raises(PermissionDeniedError):
            services.schedule(actor=blog_viewer, post=post)

    def test_a_scheduled_post_is_not_public_yet(self, api, blog_manager):
        post = make_post(status=BlogStatus.DRAFT, publish_at=tomorrow())
        services.schedule(actor=blog_manager, post=post)

        assert api.get(LIST_URL).data["count"] == 0


class TestTheScheduledPublishingJob:
    def test_publishes_a_post_whose_time_has_come(self, api):
        post = make_post(
            status=BlogStatus.SCHEDULED,
            publish_at=timezone.now() - timedelta(minutes=1),
        )

        assert tasks.publish_scheduled_posts() == 1
        post.refresh_from_db()
        assert post.status == BlogStatus.PUBLISHED
        assert api.get(LIST_URL).data["count"] == 1

    def test_leaves_a_post_whose_time_has_not_come(self):
        post = make_post(status=BlogStatus.SCHEDULED, publish_at=tomorrow())

        assert tasks.publish_scheduled_posts() == 0
        post.refresh_from_db()
        assert post.status == BlogStatus.SCHEDULED

    def test_leaves_drafts_alone_however_old_their_date(self):
        post = make_post(status=BlogStatus.DRAFT, publish_at=timezone.now() - timedelta(days=5))

        tasks.publish_scheduled_posts()
        post.refresh_from_db()

        assert post.status == BlogStatus.DRAFT

    def test_leaves_a_post_that_was_unpublished_alone(self, blog_manager):
        post = make_post(status=BlogStatus.SCHEDULED, publish_at=timezone.now() - timedelta(minutes=1))
        services.unpublish(actor=blog_manager, post=post)

        tasks.publish_scheduled_posts()
        post.refresh_from_db()

        assert post.status == BlogStatus.DRAFT

    def test_running_twice_publishes_nothing_twice(self):
        make_post(status=BlogStatus.SCHEDULED, publish_at=timezone.now() - timedelta(minutes=1))

        assert tasks.publish_scheduled_posts() == 1
        assert tasks.publish_scheduled_posts() == 0
        assert AuditEvent.objects.filter(action="blog.post.published").count() == 1

    def test_keeps_the_date_it_was_scheduled_for(self):
        when = timezone.now() - timedelta(hours=3)
        post = make_post(status=BlogStatus.SCHEDULED, publish_at=when)

        tasks.publish_scheduled_posts()
        post.refresh_from_db()

        assert post.publish_at == when

    def test_publishes_several_at_once(self):
        for index in range(3):
            make_post(
                title=f"Post {index}",
                slug=f"post-{index}",
                status=BlogStatus.SCHEDULED,
                publish_at=timezone.now() - timedelta(minutes=index + 1),
            )

        assert tasks.publish_scheduled_posts() == 3

    def test_records_who_published_it_as_the_system(self):
        make_post(status=BlogStatus.SCHEDULED, publish_at=timezone.now() - timedelta(minutes=1))

        tasks.publish_scheduled_posts()

        event = AuditEvent.objects.get(action="blog.post.published")
        assert event.actor is None
        assert event.actor_email == ""
