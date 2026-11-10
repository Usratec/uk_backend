"""Publishing through the hierarchy: who may, what is checked first, and what students then see."""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.core.models import AuditEvent, PublishStatus
from apps.learning import services
from apps.learning.models import (
    Chapter,
    ContentType,
    LearningContent,
    MediaAsset,
    MediaKind,
    Subchapter,
)
from apps.staff.authz import PermissionDeniedError

from .conftest import CONTENT, mp4_bytes

pytestmark = pytest.mark.django_db


def video_block(subchapter, slug="the-clip", title="The clip", order=1) -> LearningContent:
    asset = MediaAsset.objects.create(
        title=title,
        kind=MediaKind.VIDEO,
        file=SimpleUploadedFile(f"{slug}.mp4", mp4_bytes(), content_type="video/mp4"),
    )
    return LearningContent.objects.create(
        subchapter=subchapter,
        slug=slug,
        title=title,
        content_type=ContentType.VIDEO,
        media=asset,
        order=order,
    )


class TestPermission:
    def test_a_publisher_may_publish(self, publisher, chapter, subchapter, content):
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])

        services.publish(actor=publisher, instance=content)

        content.refresh_from_db()
        assert content.status == PublishStatus.PUBLISHED

    def test_an_editor_may_not(self, editor, subchapter, content):
        """Writing material and releasing it are separate jobs."""
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])

        with pytest.raises(PermissionDeniedError):
            services.publish(actor=editor, instance=content)

        content.refresh_from_db()
        assert content.status == PublishStatus.DRAFT

    def test_staff_with_no_learning_permissions_may_not(self, outsider, content):
        with pytest.raises(PermissionDeniedError):
            services.publish(actor=outsider, instance=content)

    def test_a_superuser_without_the_grant_may_not(self, db, content):
        """Permissions are granted on purpose here, as everywhere else in the back office."""
        from apps.accounts.models import User

        root = User.objects.create_superuser(email="root@example.com", first_name="Root")

        with pytest.raises(PermissionDeniedError):
            services.publish(actor=root, instance=content)

    def test_the_permission_follows_the_level(self, chapter, subchapter, content):
        assert services.publish_permission_for(chapter) == "learning.publish_chapter"
        assert services.publish_permission_for(subchapter) == "learning.publish_subchapter"
        assert services.publish_permission_for(content) == "learning.publish_learningcontent"


class TestChecksBeforePublishing:
    def test_empty_material_is_refused(self, publisher, subchapter):
        empty = LearningContent.objects.create(subchapter=subchapter, slug="empty", title="Empty", body_html="")
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])

        with pytest.raises(services.NotReadyToPublishError, match="no content"):
            services.publish(actor=publisher, instance=empty)

    def test_material_that_only_looks_written_is_refused(self, publisher, subchapter, content):
        """A rich-text editor leaves markup behind when staff type and delete."""
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])
        content.body_html = "<p>&nbsp;</p>"
        content.save()

        with pytest.raises(services.NotReadyToPublishError, match="no content"):
            services.publish(actor=publisher, instance=content)

    def test_material_in_an_unpublished_subchapter_is_refused(self, publisher, content):
        """Students reach material through its subchapter, so it would be unreachable."""
        with pytest.raises(services.NotReadyToPublishError, match="not published"):
            services.publish(actor=publisher, instance=content)

    def test_a_subchapter_in_an_unpublished_chapter_is_refused(self, publisher, subchapter, content):
        content.status = PublishStatus.PUBLISHED
        content.save(update_fields=["status"])

        with pytest.raises(services.NotReadyToPublishError, match="not published"):
            services.publish(actor=publisher, instance=subchapter)

    def test_an_empty_subchapter_is_refused(self, publisher, chapter, subchapter):
        """A lesson with nothing in it is one students can open and find blank."""
        chapter.status = PublishStatus.PUBLISHED
        chapter.save(update_fields=["status"])

        with pytest.raises(services.NotReadyToPublishError, match="nothing in this lesson"):
            services.publish(actor=publisher, instance=subchapter)

        subchapter.refresh_from_db()
        assert subchapter.status == PublishStatus.DRAFT

    def test_a_subchapter_whose_only_block_is_empty_is_refused(self, publisher, chapter, subchapter):
        chapter.status = PublishStatus.PUBLISHED
        chapter.save(update_fields=["status"])
        LearningContent.objects.create(subchapter=subchapter, slug="empty", title="Empty", body_html="<p>&nbsp;</p>")

        with pytest.raises(services.NotReadyToPublishError, match="nothing in this lesson"):
            services.publish(actor=publisher, instance=subchapter)

        subchapter.refresh_from_db()
        assert subchapter.status == PublishStatus.DRAFT

    def test_a_subchapter_is_published_on_a_block_that_is_still_a_draft(self, publisher, chapter, subchapter, content):
        """Written material is enough: the block itself cannot go live until the lesson has."""
        chapter.status = PublishStatus.PUBLISHED
        chapter.save(update_fields=["status"])

        services.publish(actor=publisher, instance=subchapter)

        subchapter.refresh_from_db()
        assert subchapter.is_published
        assert "nothing in this subchapter" in services.warnings_about(subchapter)[0].lower()

    def test_a_subchapter_holding_a_video_is_published(self, publisher, chapter, subchapter):
        chapter.status = PublishStatus.PUBLISHED
        chapter.save(update_fields=["status"])
        video_block(subchapter)

        services.publish(actor=publisher, instance=subchapter)

        subchapter.refresh_from_db()
        assert subchapter.is_published

    def test_a_subchapter_holding_a_question_from_the_bank_is_published(
        self, publisher, chapter, subchapter, make_question
    ):
        chapter.status = PublishStatus.PUBLISHED
        chapter.save(update_fields=["status"])
        question = make_question(key="limits")
        services.publish(actor=publisher, instance=question)
        LearningContent.objects.create(
            subchapter=subchapter,
            slug="check",
            title="Check",
            content_type=ContentType.QUESTION,
            question=question,
            order=1,
        )

        services.publish(actor=publisher, instance=subchapter)

        subchapter.refresh_from_db()
        assert subchapter.is_published

    def test_publishing_a_chapter_leaves_its_lessons_as_drafts(self, publisher, chapter, subchapter, content):
        services.publish(actor=publisher, instance=chapter)

        subchapter.refresh_from_db()
        content.refresh_from_db()
        assert subchapter.status == PublishStatus.DRAFT
        assert content.status == PublishStatus.DRAFT

    def test_an_empty_chapter_is_allowed_but_says_it_is_empty(self, publisher, chapter):
        services.publish(actor=publisher, instance=chapter)

        chapter.refresh_from_db()
        assert chapter.is_published
        assert "no subchapter" in services.warnings_about(chapter)[0].lower()

    def test_a_whole_branch_can_be_published_from_the_top_down(self, publisher, chapter, subchapter, content):
        """The order staff actually work in: a chapter, then its sections, then what goes in them.

        Every level used to wait on another — a chapter for a live subchapter, a subchapter for a
        live chapter — so nothing could be published at all. This is the test that would have
        caught it.
        """
        services.publish(actor=publisher, instance=chapter)
        services.publish(actor=publisher, instance=subchapter)
        services.publish(actor=publisher, instance=content)

        for item in (chapter, subchapter, content):
            item.refresh_from_db()
            assert item.is_published, f"{item} did not publish"

    def test_a_filled_branch_reports_no_warnings(self, publisher, chapter, subchapter, content):
        services.publish(actor=publisher, instance=chapter)
        services.publish(actor=publisher, instance=subchapter)
        services.publish(actor=publisher, instance=content)

        assert services.warnings_about(chapter) == []
        assert services.warnings_about(subchapter) == []

    def test_a_kind_of_material_we_cannot_serve_yet_is_refused(self, publisher, subchapter, monkeypatch):
        """Every kind is servable today, so the guard is exercised by withdrawing one.

        It stays because it is what stops a content type added to the model — the next kind of
        material someone thinks of — reaching students before the work to serve it exists.
        """
        monkeypatch.setattr(
            "apps.learning.services.AVAILABLE_CONTENT_TYPES", (ContentType.THEORY, ContentType.QUESTION)
        )
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])
        video = LearningContent.objects.create(
            subchapter=subchapter,
            slug="a-clip",
            title="A clip",
            content_type=ContentType.VIDEO,
            body_html=CONTENT,
        )

        with pytest.raises(services.NotReadyToPublishError, match="cannot be served yet"):
            services.publish(actor=publisher, instance=video)

    def test_a_whole_branch_publishes_from_the_bottom_up(self, publisher, chapter, subchapter, content):
        """Each level needs what is under it, so staff work upwards."""
        subchapter.status = PublishStatus.PUBLISHED
        subchapter.save(update_fields=["status"])
        services.publish(actor=publisher, instance=content)

        subchapter.status = PublishStatus.DRAFT
        subchapter.save(update_fields=["status"])
        chapter.status = PublishStatus.PUBLISHED
        chapter.save(update_fields=["status"])
        services.publish(actor=publisher, instance=subchapter)

        chapter.status = PublishStatus.DRAFT
        chapter.save(update_fields=["status"])
        services.publish(actor=publisher, instance=chapter)

        chapter.refresh_from_db()
        assert chapter.is_published


class TestScheduling:
    def test_published_material_is_live_at_once_without_a_date(self, published_tree):
        _, _, content = published_tree

        assert content.is_live() is True

    def test_material_scheduled_for_later_is_not_live_yet(self, published_tree):
        """Published is staff's decision; live is what students can actually see."""
        _, _, content = published_tree
        content.publish_from = timezone.now() + timedelta(days=2)
        content.save(update_fields=["publish_from"])

        assert content.is_published is True
        assert content.is_live() is False

    def test_it_becomes_live_once_the_time_passes(self, published_tree):
        _, _, content = published_tree
        content.publish_from = timezone.now() + timedelta(hours=1)
        content.save(update_fields=["publish_from"])

        assert content.is_live(at=timezone.now() + timedelta(hours=2)) is True

    def test_a_draft_is_never_live_even_with_a_date_in_the_past(self, content):
        content.publish_from = timezone.now() - timedelta(days=1)
        content.save(update_fields=["publish_from"])

        assert content.is_live() is False

    def test_scheduled_material_is_kept_out_of_what_students_are_served(self, published_tree):
        _, subchapter, content = published_tree
        content.publish_from = timezone.now() + timedelta(days=1)
        content.save(update_fields=["publish_from"])

        assert list(services.live_content_of(subchapter)) == []

    def test_live_material_is_served(self, published_tree):
        _, subchapter, content = published_tree

        assert list(services.live_content_of(subchapter)) == [content]


class TestRecordKeeping:
    def test_publishing_records_who_and_when(self, published_tree, publisher):
        _, _, content = published_tree

        assert content.published_by == publisher
        assert content.published_at is not None

    def test_publishing_is_audited(self, published_tree, publisher):
        event = AuditEvent.objects.get(action="learning.learningcontent.published")

        assert event.actor == publisher
        assert event.changes == {"status": {"from": "draft", "to": "published"}}

    def test_unpublishing_is_audited_with_a_reason(self, published_tree, publisher):
        _, _, content = published_tree

        services.unpublish(actor=publisher, instance=content, reason="Wrong speed limit")

        event = AuditEvent.objects.get(action="learning.learningcontent.unpublished")
        assert event.reason == "Wrong speed limit"

    def test_unpublishing_keeps_what_was_written(self, published_tree, publisher):
        _, _, content = published_tree

        services.unpublish(actor=publisher, instance=content)

        content.refresh_from_db()
        assert content.status == PublishStatus.DRAFT
        assert content.body_html == CONTENT


class TestOrdering:
    def test_positions_are_numbered_from_one_with_no_gaps(self, editor, chapter):
        first = Subchapter.objects.create(chapter=chapter, slug="a", title="A", order=5)
        second = Subchapter.objects.create(chapter=chapter, slug="b", title="B", order=9)

        services.reorder(actor=editor, model=Subchapter, ordered_ids=[second.pk, first.pk])

        second.refresh_from_db()
        first.refresh_from_db()
        assert (second.order, first.order) == (1, 2)

    def test_renumbering_one_chapter_leaves_another_alone(self, editor, chapter):
        """Positions are counted within a parent, so chapters do not interfere."""
        other = Chapter.objects.create(slug="motorways", title="Motorways", order=2)
        mine = Subchapter.objects.create(chapter=chapter, slug="a", title="A", order=1)
        theirs = Subchapter.objects.create(chapter=other, slug="b", title="B", order=1)

        services.reorder(actor=editor, model=Subchapter, ordered_ids=[mine.pk])

        theirs.refresh_from_db()
        assert theirs.order == 1

    def test_staff_without_the_change_permission_may_not_reorder(self, outsider, chapter):
        with pytest.raises(PermissionDeniedError):
            services.reorder(actor=outsider, model=Chapter, ordered_ids=[chapter.pk])


class TestWhatTheApiWillServe:
    def test_a_subchapter_is_served_with_its_material_as_blocks(self, published_tree):
        """The frontend keeps its own words: a chapter is a topic, a subchapter is a lesson."""
        chapter, subchapter, content = published_tree

        view = services.student_view_of(subchapter)

        assert view["slug"] == subchapter.slug
        assert view["topic"] == {"slug": chapter.slug, "title": chapter.title}
        assert view["blocks"] == [{"type": "html", "html": CONTENT, "title": content.title}]

    def test_material_still_in_draft_is_left_out(self, published_tree, subchapter):
        LearningContent.objects.create(
            subchapter=subchapter, slug="draft-piece", title="Draft piece", body_html="<p>Later.</p>"
        )

        assert len(services.student_view_of(subchapter)["blocks"]) == 1

    def test_blocks_come_in_the_order_staff_set(self, published_tree, subchapter, publisher):
        _, _, first = published_tree
        second = LearningContent.objects.create(
            subchapter=subchapter, slug="second", title="Second", body_html="<p>Second.</p>", order=2
        )
        services.publish(actor=publisher, instance=second)

        titles = [block["title"] for block in services.student_view_of(subchapter)["blocks"]]

        assert titles == [first.title, "Second"]
