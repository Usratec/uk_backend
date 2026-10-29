"""Publishing and ordering learning material.

Saving content and showing it to students are deliberately separate actions. Everything starts as
a draft; publishing is its own step, with its own permission, that checks the material will
actually work before anybody can reach it, and records who released it.

The admin calls these functions. So will the student API, which is why the rules live here rather
than in the admin screens.
"""

import hashlib
import logging
from decimal import ROUND_DOWN, Decimal

from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from apps.core import audit, images
from apps.core.models import PublishStatus
from apps.learning import hazard_detection, html
from apps.learning.models import (
    AVAILABLE_CONTENT_TYPES,
    Chapter,
    ContentType,
    Ebook,
    EbookSection,
    EbookSectionProgress,
    ExamKind,
    HazardAttempt,
    HazardClip,
    HazardWindow,
    LearningContent,
    LessonProgress,
    MediaAsset,
    MediaKind,
    MockAttempt,
    PracticeExam,
    Question,
    QuestionAttempt,
    QuestionType,
    SavedQuestion,
    Sign,
    Subchapter,
    VideoProgress,
)
from apps.notifications import services as notifications
from apps.staff.authz import require_permission

logger = logging.getLogger(__name__)


class NotReadyToPublishError(Exception):
    """The material would not work for students."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def publish_permission_for(instance) -> str:
    """The permission needed to publish this kind of material, e.g. ``learning.publish_chapter``."""
    meta = instance._meta
    return f"{meta.app_label}.publish_{meta.model_name}"


# --- What students can see -----------------------------------------------------------------------


def live(queryset, at=None):
    """Narrow a queryset to what students can see now.

    Published is not the same as visible: material can be published today and scheduled to appear
    next Monday, and until then it stays out of sight.
    """
    at = at or timezone.now()
    return queryset.filter(status=PublishStatus.PUBLISHED).filter(
        Q(publish_from__isnull=True) | Q(publish_from__lte=at)
    )


def live_subchapters_of(chapter: Chapter, at=None):
    return live(chapter.subchapters.all(), at)


def live_content_of(subchapter: Subchapter, at=None):
    return live(subchapter.content.all(), at)


# --- Checks before anything reaches students -----------------------------------------------------


def problems_with(instance) -> list[str]:
    """Everything that would stop this being published. Empty means it is ready."""
    if isinstance(instance, Chapter):
        return _problems_with_chapter(instance)
    if isinstance(instance, Subchapter):
        return _problems_with_subchapter(instance)
    if isinstance(instance, LearningContent):
        return _problems_with_content(instance)
    if isinstance(instance, Question):
        return problems_with_question(instance)
    if isinstance(instance, PracticeExam):
        return problems_with_exam(instance)
    if isinstance(instance, HazardClip):
        return problems_with_clip(instance)
    if isinstance(instance, Ebook):
        return problems_with_ebook(instance)
    if isinstance(instance, EbookSection):
        return problems_with_ebook_section(instance)
    return []


def problems_with_ebook(ebook: Ebook) -> list[str]:
    problems = []
    document = ebook.file

    if document is not None and document.kind != MediaKind.DOCUMENT:
        problems.append(f"'{document.title}' is {document.get_kind_display().lower()}, not a document.")

    if not ebook.slug:
        problems.append("There is no slug yet, so the e-book has no address to be read at.")

    return problems


def problems_with_ebook_section(section: EbookSection) -> list[str]:
    """What would make a section of a book unreadable."""
    problems = []

    document = section.media

    if html.is_empty(section.body_html) and document is None:
        problems.append("There is nothing in this section yet: write something, or choose a file.")

    if document is not None and MediaKind(document.kind) not in EBOOK_MEDIA_KINDS:
        problems.append(
            f"'{document.title}' is {document.get_kind_display().lower()}. "
            "A section shows a video or a document; put pictures in the text itself."
        )

    unknown = unknown_sign_codes(section.body_html)
    if unknown:
        listed = ", ".join(unknown[:5])
        problems.append(f"The text asks for signs that are not in the library: {listed}.")

    missing = missing_media_references(section.body_html)
    if missing:
        listed = ", ".join(str(asset_id) for asset_id in missing[:5])
        problems.append(
            f"The text shows pictures that are no longer in the media library (assets {listed}). "
            "Remove them from the text, or upload them again."
        )

    if not section.ebook.is_published:
        problems.append(f"Its book '{section.ebook.title}' is not published, so students cannot reach it.")

    return problems


def problems_with_clip(clip: HazardClip) -> list[str]:
    """What would make a hazard clip unusable."""
    problems = []
    windows = list(clip.windows.all())

    if clip.media.kind != MediaKind.VIDEO:
        problems.append(f"'{clip.media.title}' is not a video, so there is nothing to watch.")

    if not windows:
        # Without one there is nothing to spot, and every attempt would score nothing.
        problems.append("This clip has no hazards marked yet.")

    # Overlapping windows would let one click score for two hazards at once.
    in_order = sorted(windows, key=lambda window: window.starts_at)
    for earlier, later in zip(in_order, in_order[1:], strict=False):
        if later.starts_at < earlier.ends_at:
            problems.append(f"'{earlier.label}' and '{later.label}' overlap, so one click would score twice.")

    return problems


def _problems_with_chapter(chapter: Chapter) -> list[str]:
    # Nothing can stop a chapter going live. It is the top of the hierarchy, so it has no parent
    # to be unreachable behind, and being empty is a warning rather than a refusal — see
    # :func:`warnings_about`.
    return []


def _problems_with_subchapter(subchapter: Subchapter) -> list[str]:
    if not subchapter.chapter.is_published:
        return [f"Its chapter '{subchapter.chapter.title}' is not published, so students cannot reach it."]
    return []


def warnings_about(instance) -> list[str]:
    """What is worth knowing before publishing, but is not a reason to refuse.

    The difference matters because of the order people work in. Material is built from the top
    down — a chapter, then its subchapters, then what goes in them — so at the moment a chapter
    is published it is always empty, and refusing to publish an empty one made it impossible to
    publish anything at all: the chapter wanted a live subchapter, the subchapter wanted a live
    chapter, and neither could go first.

    So "there is nothing in here yet" is said rather than enforced. It costs nothing to be wrong
    about, because the API only ever returns live children: an empty published chapter shows
    students no lessons rather than a broken one.

    What stays a refusal is the other direction — publishing something students could not reach,
    because its parent is still a draft. That one cannot be worked around by filling it in later.
    """
    if isinstance(instance, Chapter):
        if not live_subchapters_of(instance).exists():
            return ["No subchapter in this chapter is published yet, so students will see it empty."]
    if isinstance(instance, Subchapter):
        if not live_content_of(instance).exists():
            return ["Nothing in this subchapter is published yet, so students will see it empty."]
    if isinstance(instance, PracticeExam):
        if instance.hazard_pass_mark is None and instance.hazard_clips.exists():
            if not instance.questions.exists():
                return ["This test is clips only and has no hazard pass mark, so every student will pass it."]
            return ["This test has clips but no hazard pass mark, so students pass it on the questions alone."]
    if isinstance(instance, Ebook):
        if instance.file_id is None and not ebook_sections(instance).exists():
            return ["No section of this book is published yet and it has no document, so students will see it empty."]
    if isinstance(instance, HazardClip):
        if instance.duration_seconds is None:
            return ["This clip has no length, so clicking spread evenly across it will not be spotted."]
    return []


def problems_with_question(question: Question) -> list[str]:
    """What would make a question unusable for a student."""
    problems = []
    options = list(question.options.all())
    correct = [option for option in options if option.is_correct]

    if len(options) < 2:
        problems.append("A question needs at least two answers to choose between.")

    if question.question_type == QuestionType.MULTI:
        if len(correct) < 2:
            problems.append("A question with several answers needs at least two of them marked correct.")
    elif len(correct) != 1:
        # Without exactly one, the frontend cannot tell the student what the answer was.
        problems.append("Exactly one answer must be marked correct.")

    if question.question_type == QuestionType.IMAGE and any(
        option.sign_id is None and option.image_id is None for option in options
    ):
        problems.append(
            "Every answer needs a road sign or a picture, because this question is answered with pictures."
        )

    # Word answers render as buttons. A picture question is answered with pictures, so its text
    # may be empty; a word question with nothing in the button is not usable.
    if question.question_type in {QuestionType.SINGLE, QuestionType.MULTI} and any(
        not option.text.strip() for option in options
    ):
        problems.append("Every answer needs text.")

    if not question.prompt.strip():
        problems.append("There is no question text yet.")

    return problems


def problems_with_exam(exam: PracticeExam) -> list[str]:
    """What would make an exam unusable."""
    problems = []
    questions = list(exam.questions.all())
    clips = list(exam.hazard_clips.all())

    if exam.kind == ExamKind.MOCK:
        if not questions:
            problems.append("A mock test stands in for the real test, so it needs questions as well as clips.")
        if not clips:
            problems.append("A mock test stands in for the real test, so it needs hazard clips as well as questions.")
    elif not questions and not clips:
        problems.append("This exam has no questions or hazard clips yet. Add at least one of either.")

    drafts = [question for question in questions if not question.is_published]
    if drafts:
        # Naming them, so staff can go and fix the right ones.
        listed = ", ".join(question.key for question in drafts[:5])
        problems.append(f"Some questions are not published yet: {listed}.")

    if questions and exam.pass_mark is None:
        problems.append("This paper asks questions, so it needs a pass mark.")
    elif exam.pass_mark and exam.pass_mark > len(questions):
        problems.append(f"The pass mark is {exam.pass_mark} but there are only {len(questions)} questions.")

    hazard_marks = sum(clip.top_score for clip in clips)
    if exam.hazard_pass_mark and exam.hazard_pass_mark > hazard_marks:
        # Otherwise the clips could not be passed however early every hazard was spotted.
        problems.append(
            f"The hazard pass mark is {exam.hazard_pass_mark} but the clips are only worth {hazard_marks}."
        )

    draft_clips = [clip for clip in clips if not clip.is_published]
    if draft_clips:
        listed = ", ".join(clip.title for clip in draft_clips[:5])
        problems.append(f"Some hazard clips are not published yet: {listed}.")

    return problems


def _problems_with_content(content: LearningContent) -> list[str]:
    problems = []

    if content.content_type not in AVAILABLE_CONTENT_TYPES:
        kind = ContentType(content.content_type).label
        problems.append(f"{kind} material cannot be served yet, so it cannot be published.")
    elif content.content_type == ContentType.QUESTION:
        if content.question is None:
            problems.append("No question has been chosen from the bank.")
        elif not content.question.is_published:
            problems.append(f"The question '{content.question.key}' is not published yet.")
    elif content.content_type == ContentType.HAZARD:
        if content.hazard_clip is None:
            problems.append("No hazard clip has been chosen.")
        elif not content.hazard_clip.is_published:
            problems.append(f"The clip '{content.hazard_clip.title}' is not published yet.")
    elif content.content_type in MEDIA_CONTENT_TYPES:
        wanted = MEDIA_CONTENT_TYPES[ContentType(content.content_type)]
        if content.media is None:
            problems.append("No file has been chosen from the media library.")
        elif content.media.kind != wanted:
            # Otherwise the frontend would be told to play a PDF, or to download a video.
            problems.append(
                f"'{content.media.title}' is {content.media.get_kind_display().lower()}, "
                f"but this is {ContentType(content.content_type).label.lower()} material."
            )
    elif html.is_empty(content.body_html):
        problems.append("There is no content yet.")

    missing = missing_media_references(content.body_html)
    if missing:
        listed = ", ".join(str(asset_id) for asset_id in missing[:5])
        problems.append(
            f"The text shows pictures that are no longer in the media library (assets {listed}). "
            "Remove them from the text, or upload them again."
        )

    if not content.subchapter.is_published:
        problems.append(f"Its subchapter '{content.subchapter.title}' is not published, so students cannot reach it.")

    return problems


# --- Actions -------------------------------------------------------------------------------------


@transaction.atomic
def publish(*, actor, instance, reason: str = ""):
    """Release material to students. Raises if the actor may not, or it is not ready."""
    require_permission(actor, publish_permission_for(instance))

    problems = problems_with(instance)
    if problems:
        raise NotReadyToPublishError(problems)

    instance.status = PublishStatus.PUBLISHED
    instance.published_at = timezone.now()
    instance.published_by = actor
    instance.save(update_fields=["status", "published_at", "published_by", "updated_at"])

    audit.record(
        actor=actor,
        action=f"learning.{instance._meta.model_name}.published",
        target=instance,
        changes={"status": {"from": "draft", "to": "published"}},
        reason=reason,
    )
    logger.info("%s published %s %s", getattr(actor, "email", "system"), instance._meta.model_name, instance.pk)
    return instance


@transaction.atomic
def unpublish(*, actor, instance, reason: str = ""):
    """Take material back off the site. It becomes a draft again, keeping what was written."""
    require_permission(actor, publish_permission_for(instance))

    was = instance.status
    instance.status = PublishStatus.DRAFT
    instance.save(update_fields=["status", "updated_at"])

    audit.record(
        actor=actor,
        action=f"learning.{instance._meta.model_name}.unpublished",
        target=instance,
        changes={"status": {"from": was, "to": "draft"}},
        reason=reason,
    )
    return instance


@transaction.atomic
def reorder(*, actor, model, ordered_ids: list[int]) -> None:
    """Put material in the given order, numbering it 1, 2, 3 with no gaps.

    Positions are counted within one parent, so renumbering a chapter never disturbs another.
    """
    require_permission(actor, f"{model._meta.app_label}.change_{model._meta.model_name}")

    by_id = {item.pk: item for item in model.objects.filter(pk__in=ordered_ids)}
    changed = []
    for position, item_id in enumerate(ordered_ids, start=1):
        item = by_id.get(item_id)
        if item is not None and item.order != position:
            item.order = position
            changed.append(item)

    if changed:
        model.objects.bulk_update(changed, ["order"])


IMAGE_UPLOAD_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
}

MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024


class ImageUploadError(Exception):
    """The file staff chose cannot be used as a picture — beside a question, or inside content."""


def check_image_upload(upload) -> None:
    extension = (upload.name or "").rsplit(".", 1)[-1].lower() if "." in (upload.name or "") else ""
    if extension not in IMAGE_UPLOAD_TYPES:
        allowed = ", ".join(sorted({name.upper() for name in IMAGE_UPLOAD_TYPES}))
        raise ImageUploadError(f"Only {allowed} files can be used as a picture.")

    declared = (getattr(upload, "content_type", "") or "").lower()
    if declared and declared not in set(IMAGE_UPLOAD_TYPES.values()):
        raise ImageUploadError(f"'{declared}' is not a picture this site can show.")

    if (upload.size or 0) > MAX_IMAGE_UPLOAD_BYTES:
        raise ImageUploadError(f"The picture must be under {MAX_IMAGE_UPLOAD_BYTES // (1024 * 1024)} MB.")


IMAGE_UPLOAD_FORMATS = {"JPEG", "PNG", "WEBP", "GIF"}


def _verify_is_an_image(upload) -> None:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(upload) as image:
            image_format = image.format
            image.verify()
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise ImageUploadError("That file is not a picture this site can show.") from error
    finally:
        upload.seek(0)

    if image_format not in IMAGE_UPLOAD_FORMATS:
        raise ImageUploadError(f"{image_format or 'That file'} is not a picture this site can show.")


def _checksum_of(upload) -> str:
    digest = hashlib.sha256()
    for chunk in upload.chunks():
        digest.update(chunk)
    upload.seek(0)
    return digest.hexdigest()


def image_asset_from_upload(*, upload, uploaded_by=None, title: str = "") -> MediaAsset:
    check_image_upload(upload)
    _verify_is_an_image(upload)

    original_name = upload.name
    try:
        upload = images.without_metadata(upload)
    except images.ImageMetadataError as error:
        raise ImageUploadError(str(error)) from error

    checksum = _checksum_of(upload)
    existing = MediaAsset.objects.filter(checksum=checksum, kind=MediaKind.IMAGE).order_by("pk").first()
    if existing is not None:
        return existing

    asset = MediaAsset(
        title=(title or original_name or "Picture")[:160],
        kind=MediaKind.IMAGE,
        file=upload,
        uploaded_by=uploaded_by,
    )
    asset.checksum = checksum
    asset.save()
    return asset


def image_url_of(asset: MediaAsset | None) -> str | None:
    return asset.file.url if asset and asset.file else None


MAX_SUBTITLE_UPLOAD_BYTES = 2 * 1024 * 1024

WEBVTT_SIGNATURE = "WEBVTT"


class SubtitleUploadError(Exception):
    """The file staff chose cannot be used as a subtitle track."""


def check_subtitle_upload(upload) -> None:
    name = upload.name or ""
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension != "vtt":
        raise SubtitleUploadError("Subtitles must be a WebVTT (.vtt) file.")

    if (upload.size or 0) > MAX_SUBTITLE_UPLOAD_BYTES:
        raise SubtitleUploadError(f"The subtitle file must be under {MAX_SUBTITLE_UPLOAD_BYTES // (1024 * 1024)} MB.")

    _verify_is_webvtt(upload)


def _verify_is_webvtt(upload) -> None:
    try:
        head = upload.read(64)
    finally:
        upload.seek(0)

    if isinstance(head, bytes):
        head = head.decode("utf-8-sig", errors="replace")
    head = head.lstrip("﻿")

    signature, _, rest = head.partition("\n")
    signature = signature.rstrip("\r")
    if not signature.startswith(WEBVTT_SIGNATURE) or signature[len(WEBVTT_SIGNATURE) :][:1] not in ("", " ", "\t"):
        raise SubtitleUploadError(
            "That file is not WebVTT subtitles: a .vtt file starts with the word WEBVTT on its first line."
        )


def subtitles_of(asset: MediaAsset | None) -> list[dict]:
    if asset is None:
        return []
    return [
        {"language": track.language, "label": track.get_language_display(), "url": track.file.url}
        for track in asset.subtitles.all()
        if track.file
    ]


def resolved_media_in(body_html: str | None) -> str:
    asset_ids = html.media_ids_in(body_html)
    if not asset_ids:
        return body_html or ""

    urls = {
        asset.pk: url
        for asset in MediaAsset.objects.filter(pk__in=asset_ids, kind=MediaKind.IMAGE)
        if (url := image_url_of(asset))
    }
    return html.with_media_urls(body_html, urls)


def missing_media_references(body_html: str | None) -> list[int]:
    asset_ids = html.media_ids_in(body_html)
    if not asset_ids:
        return []

    found = set(MediaAsset.objects.filter(pk__in=asset_ids, kind=MediaKind.IMAGE).values_list("pk", flat=True))
    return [asset_id for asset_id in asset_ids if asset_id not in found]


def question_media_of(question: Question) -> dict | None:
    if question.media_sign:
        return {"kind": "sign", "code": question.media_sign.code}
    if question.media_image:
        return {"kind": "image", "url": image_url_of(question.media_image)}
    return None


# --- What the API will serve ---------------------------------------------------------------------


def student_view_of(instance) -> dict:
    """The shape of this material for the student API, also used by the admin preview.

    Built here rather than in the preview page, so the preview cannot drift away from what is
    actually served. The API keeps its existing names — a chapter is a "topic" to the frontend and
    a subchapter is a "lesson" — so the rename inside the product does not reach it.

    This leaves out drafts and anything not yet live, but not material limited to packages: that
    depends on who is asking, so the API view narrows it with ``entitlements.visible_to`` for the
    signed-in student. Staff previewing see everything that is live.
    """
    if isinstance(instance, Subchapter):
        return {
            "slug": instance.slug,
            "title": instance.title,
            "summary": instance.summary,
            "minutes": instance.minutes,
            "topic": {"slug": instance.chapter.slug, "title": instance.chapter.title},
            "blocks": [block_for(item) for item in live_content_of(instance).order_by("order", "title")],
        }

    if isinstance(instance, LearningContent):
        return {
            "slug": instance.slug,
            "title": instance.title,
            "blocks": [block_for(instance)],
        }

    if isinstance(instance, HazardClip):
        # Nothing to read: what staff need to check is the video and where its hazards sit.
        return {
            "slug": instance.slug,
            "title": instance.title,
            "summary": instance.description,
            "blocks": [],
        }

    if isinstance(instance, Ebook):
        return {
            "slug": instance.slug,
            "title": instance.title,
            "summary": instance.description,
            "blocks": [],
        }

    if isinstance(instance, EbookSection):
        return {
            "slug": instance.slug,
            "title": instance.title,
            "summary": instance.summary,
            "topic": {"slug": instance.ebook.slug, "title": instance.ebook.title},
            "blocks": blocks_of_section(instance),
        }

    if isinstance(instance, Question):
        # A question has no body to render, so the preview page shows the answer sheet instead.
        return {
            "slug": instance.key,
            "title": instance.prompt,
            "summary": "",
            "blocks": [],
        }

    return {
        "slug": instance.slug,
        "title": instance.title,
        "summary": getattr(instance, "description", ""),
        "blocks": [],
    }


def answer_sheet_for(question: Question) -> dict:
    """A question with its answers marked, for staff checking it before publishing.

    Deliberately a separate function from :func:`student_view_of_question`, which never carries
    the answer, so a page meant for staff cannot be wired to students by mistake.
    """
    return {
        "prompt": question.prompt,
        "sign": question.media_sign.code if question.media_sign else "",
        "image": image_url_of(question.media_image),
        "explanation": question.explanation,
        "learn_more": question.learn_more.title if question.learn_more else "",
        "options": [
            {
                "id": option.option_id,
                "text": option.text,
                "sign": option.sign.code if option.sign else "",
                "image": image_url_of(option.image),
                "is_correct": option.is_correct,
            }
            for option in question.options.all()
        ],
    }


EBOOK_MEDIA_KINDS = {
    MediaKind.VIDEO: "video",
    MediaKind.DOCUMENT: "document",
}

#: Which kind of file each media content type expects, so one cannot be published with the other.
MEDIA_CONTENT_TYPES = {
    ContentType.VIDEO: MediaKind.VIDEO,
    ContentType.DOCUMENT: MediaKind.DOCUMENT,
}


def block_for(content: LearningContent, progress: dict | None = None) -> dict:
    """One content item as the frontend reads it.

    The frontend takes content as a list of blocks: theory is one HTML block, a question is a
    check block naming the question, and a file is a video or document block carrying a URL.

    That URL is signed and expires, and it is built here — at the moment of serving — rather than
    stored, so a link cannot be kept or shared beyond its lifetime. The caller has already decided
    this student may see the content, which is what earns them the URL.
    """
    if content.content_type == ContentType.QUESTION and content.question:
        return {"type": "check", "questions": [content.question.key], "title": content.title}

    if content.content_type == ContentType.HAZARD and content.hazard_clip:
        return {
            "type": "hazard",
            "title": content.title,
            "clips": [content.hazard_clip.slug],
        }

    if content.content_type in MEDIA_CONTENT_TYPES and content.media:
        block: dict = {
            "type": content.content_type,
            "title": content.title,
            "url": content.media.file.url,
        }
        if content.content_type == ContentType.VIDEO:
            block["slug"] = content.slug
            block["subtitles"] = subtitles_of(content.media)
            block["progress"] = progress if progress is not None else video_position_of(None)
        return block

    return {"type": "html", "html": resolved_media_in(content.body_html), "title": content.title}


def student_view_of_question(question: Question) -> dict:
    """A question as a student sees it before answering.

    Built without ``is_correct``: the answer is not in the payload at all, rather than being sent
    and hidden by the frontend.
    """
    return {
        "id": question.pk,
        "key": question.key,
        "topic": question.chapter.slug,
        "type": question.question_type,
        "prompt": question.prompt,
        "media": question_media_of(question),
        "options": [
            {
                "id": option.option_id,
                **({"text": option.text} if option.text else {}),
                **({"sign": option.sign.code} if option.sign else {}),
                **({"image": image_url_of(option.image)} if option.image_id else {}),
            }
            for option in question.options.all()
        ],
        "pick": question.pick,
    }


def questions_in_exam(exam: PracticeExam, at=None):
    """The exam's published questions, in the order staff put them in.

    Drafts are left out rather than shown unanswerable, which is also why an exam cannot be
    published while any of its questions is still a draft — see :func:`problems_with_exam`.
    """
    return live(
        Question.objects.filter(exam_places__exam=exam)
        .select_related("chapter", "media_sign", "media_image")
        .prefetch_related("options__sign", "options__image"),
        at=at,
    ).order_by("exam_places__order")


def clips_in_exam(exam: PracticeExam, at=None):
    return live(
        HazardClip.objects.filter(exam_places__exam=exam)
        .select_related("media")
        .prefetch_related("windows", "media__subtitles"),
        at=at,
    ).order_by("exam_places__order")


def hazard_totals_for(exams, at=None) -> dict[int, dict]:
    """How many live clips each exam has, and what they are worth, in one query.

    One query for the whole listing rather than two per exam: the numbers are small but the
    database is not local, so it is the round trips that cost — the same reason the course
    listing works this way.

    Exams with no clips are simply absent, which the caller reads as none.
    """
    rows = (
        live(HazardClip.objects.filter(exam_places__exam__in=exams), at=at)
        .values_list("exam_places__exam_id")
        .annotate(clips=Count("id", distinct=True), hazards=Count("windows", distinct=True))
    )
    return {
        exam_id: {"clipCount": clips, "hazardMaxScore": hazards * HazardWindow.BANDS}
        for exam_id, clips, hazards in rows
    }


def statistics(*, total: int, correct: int, unanswered: int, hazard_score: int, hazard_max: int) -> dict:
    return {
        "totalQuestions": total,
        "correctQuestions": correct,
        "incorrectQuestions": max(total - correct - unanswered, 0),
        "unansweredQuestions": unanswered,
        "hazardTotalScore": hazard_score,
        "hazardMaxScore": hazard_max,
    }


def attempt_statistics(attempt: MockAttempt) -> dict:
    return statistics(
        total=attempt.total,
        correct=attempt.score,
        unanswered=attempt.unanswered,
        hazard_score=attempt.hazard_score,
        hazard_max=attempt.hazard_top_score,
    )


# --- Sittings a student can look back at ---------------------------------------------------------
#
# A sitting is finished the moment it is marked, so every recorded attempt is a completed one and
# there is no half-finished state to filter out. What the history is read from is the attempt rows
# themselves, which are never edited, so the list cannot disagree with the results it links to.


def attempts_of(student, exam: PracticeExam):
    """This student's sittings of one exam, newest first."""
    return MockAttempt.objects.filter(student=student, exam=exam).order_by("-created_at", "-id")


def attempt_in_the_history(attempt: MockAttempt) -> dict:
    """One sitting as the history lists it: the headline marks, and the id to open it by."""
    return {
        "id": attempt.pk,
        "completedAt": attempt.created_at,
        "score": attempt.score,
        "total": attempt.total,
        "passed": attempt.passed,
        "percent": percent_of(attempt.score, attempt.total),
        "hazardScore": attempt.hazard_score,
        "hazardMaxScore": attempt.hazard_top_score,
        "timeTakenSeconds": attempt.time_taken_seconds,
    }


def percent_of(score: int, total: int) -> int:
    """A score as a whole percentage. Nothing out of nothing is nothing rather than an error."""
    return round(score * 100 / total) if total else 0


def history_for(student, exams) -> dict[int, dict]:
    """How each listed exam has gone for this student, in one query for the whole listing.

    Per exam: how many times they have sat it, the best percentage they have managed and the id of
    the latest sitting, so the listing can offer "retake" and "see last results" without asking
    about every exam separately — the same arrangement as the hazard totals.
    """
    if not exams:
        return {}

    history: dict[int, dict] = {}
    # Newest last, so the latest sitting is simply the one left in `lastAttemptId`.
    rows = (
        MockAttempt.objects.filter(student=student, exam__in=exams)
        .order_by("created_at", "id")
        .values_list("exam_id", "id", "score", "total")
    )
    for exam_id, attempt_id, score, total in rows:
        seen = history.setdefault(exam_id, {"attempts": 0, "bestPercent": 0, "lastAttemptId": None})
        seen["attempts"] += 1
        seen["bestPercent"] = max(seen["bestPercent"], percent_of(score, total))
        seen["lastAttemptId"] = attempt_id
    return history


ALL_ATTEMPTS_LIMIT = 50


def all_attempts_of(student, limit: int = ALL_ATTEMPTS_LIMIT) -> list[MockAttempt]:
    return list(
        MockAttempt.objects.filter(student=student).select_related("exam").order_by("-created_at", "-id")[:limit]
    )


def attempt_across_exams(attempt: MockAttempt) -> dict:
    saved = attempt.result or {}
    return {
        **attempt_in_the_history(attempt),
        "exam": {
            "id": attempt.exam_id,
            "slug": attempt.exam.slug,
            "title": attempt.exam.title,
            "kind": attempt.exam.kind,
        },
        "questionsPassed": saved.get("questionsPassed", attempt.passed),
        "hazardPassed": saved.get("hazardPassed"),
    }


def saved_result_of(attempt: MockAttempt) -> dict:
    saved = dict(attempt.result or {})
    if not saved:
        saved = {
            "score": attempt.score,
            "total": attempt.total,
            "passMark": attempt.exam.pass_mark,
            "hazardPassMark": attempt.exam.hazard_pass_mark,
            "passed": attempt.passed,
            "questions": [],
            "clips": [],
            **attempt_statistics(attempt),
        }

    saved.setdefault("questionsPassed", saved.get("passed", attempt.passed))
    saved.setdefault("hazardPassed", None)

    return {
        **saved,
        "attemptId": attempt.pk,
        "timeTakenSeconds": attempt.time_taken_seconds,
        "completedAt": attempt.created_at,
        "exam": {"slug": attempt.exam.slug, "title": attempt.exam.title, "kind": attempt.exam.kind},
    }


def hazard_result_of(exam: PracticeExam, hazard_score: int, hazard_max: int) -> bool | None:
    """Whether a sitting passed the clips.

    None when there is nothing to judge: a question-only paper, or an exam whose hazard pass mark
    has not been decided. A sitting that cannot be judged on the clips is not failed by them.
    """
    if not hazard_max or exam.hazard_pass_mark is None:
        return None
    return hazard_score >= exam.hazard_pass_mark


def mark_exam(exam: PracticeExam, answers: dict[str, list[str]], clicks: dict[str, list[float]] | None = None) -> dict:
    """Score a whole sitting, which is the only place a pass or fail is decided.

    ``answers`` maps a question id to the option ids chosen for it. Anything not answered counts
    as wrong, so a student cannot improve a score by leaving questions out, and unknown ids are
    ignored rather than rejected: a question unpublished mid-sitting should not void the attempt.
    """
    results = []
    score = 0
    unanswered = 0

    for question in questions_in_exam(exam):
        chosen = answers.get(str(question.pk)) or []
        correct_ids = question.correct_option_ids
        correct = sorted(chosen) == sorted(correct_ids)
        score += correct
        unanswered += not chosen
        results.append(
            {
                "id": question.pk,
                "key": question.key,
                "correct": correct,
                "selected": chosen,
                "correctIds": correct_ids,
                "explanation": question.explanation,
            }
        )

    clips, hazard_score, hazard_max = _mark_clips(exam, clicks or {})

    questions_passed = score >= (exam.pass_mark or 0)
    hazard_passed = hazard_result_of(exam, hazard_score, hazard_max)

    return {
        "score": score,
        "total": len(results),
        "passMark": exam.pass_mark,
        "hazardPassMark": exam.hazard_pass_mark,
        "questionsPassed": questions_passed,
        "hazardPassed": hazard_passed,
        "passed": questions_passed and hazard_passed is not False,
        "questions": results,
        "clips": clips,
        **statistics(
            total=len(results),
            correct=score,
            unanswered=unanswered,
            hazard_score=hazard_score,
            hazard_max=hazard_max,
        ),
    }


def _mark_clips(exam: PracticeExam, clicks: dict[str, list[float]]) -> tuple[list[dict], int, int]:
    scored: list[dict] = []
    score = 0
    top_score = 0

    for clip in clips_in_exam(exam):
        marked = score_clip(clip, [float(moment) for moment in clicks.get(clip.slug) or []])
        score += marked["score"]
        top_score += marked["topScore"]
        scored.append({"id": clip.pk, "slug": clip.slug, "title": clip.title, **marked})

    return scored, score, top_score


def mark_answer(question: Question, selected: list[str]) -> dict:
    """Mark an answer, which is the only place correctness is decided.

    Marking happens here rather than in the browser, so the answer is never in the page before
    the student commits to one.
    """
    correct_ids = question.correct_option_ids
    return {
        "correct": sorted(selected) == sorted(correct_ids),
        "correctIds": correct_ids,
        "explanation": question.explanation,
        "lesson": (
            {"slug": question.learn_more.slug, "title": question.learn_more.title} if question.learn_more else None
        ),
    }


# --- Progress ------------------------------------------------------------------------------------
#
# Everything here is worked out from the attempt records rather than kept in a counter, so a
# number shown to a student can never drift away from what they actually did.


def record_answer(student, question: Question, was_correct: bool) -> None:
    """Remember one answer."""
    QuestionAttempt.objects.create(student=student, question=question, was_correct=was_correct)
    notifications.queue_streak_check(student)


TIME_TAKEN_GRACE_SECONDS = 30
"""How far past the time limit a reported timing is still believed.

The last question is answered and only then is the paper posted, so a sitting that ran the timer
down can honestly report a few seconds more than the limit. Past this grace the figure is a clock
that drifted or was tampered with, and it is cut back.
"""


TIME_TAKEN_CEILING_SECONDS = 24 * 60 * 60
"""The most an untimed exam can be reported as having taken: a day.

An exam with no time limit has nothing of its own to clamp against, and a figure wider than the
column would fail the insert and lose the marks with it, so there is still a ceiling.
"""


def time_taken_for(exam: PracticeExam, seconds: int | None) -> int | None:
    if seconds is None:
        return None
    if exam.time_limit_seconds is None:
        ceiling = TIME_TAKEN_CEILING_SECONDS
    else:
        ceiling = exam.time_limit_seconds + TIME_TAKEN_GRACE_SECONDS
    return min(max(seconds, 0), ceiling)


@transaction.atomic
def record_sitting(student, exam: PracticeExam, result: dict, time_taken_seconds: int | None = None) -> MockAttempt:
    """Remember one exam sitting: the score, and every answer and clip that made it up.

    The answers are recorded too, so a question got wrong in a mock turns up under "mistakes"
    exactly as one got wrong while practising — and each clip is recorded as a hazard attempt for
    the same reason: a clip sat in a mock is a go at that clip, and leaving it out would make a
    student's hazard history disagree with the mocks they have sat.

    Atomic, so a sitting is never half recorded: the attempt, its answers and its clips are one
    record of one event.
    """
    attempt = MockAttempt.objects.create(
        student=student,
        exam=exam,
        score=result["score"],
        total=result["total"],
        passed=result["passed"],
        unanswered=result["unansweredQuestions"],
        hazard_score=result["hazardTotalScore"],
        hazard_top_score=result["hazardMaxScore"],
        time_taken_seconds=time_taken_for(exam, time_taken_seconds),
        result=result,
    )
    QuestionAttempt.objects.bulk_create(
        QuestionAttempt(student=student, question_id=answer["id"], was_correct=answer["correct"])
        for answer in result["questions"]
    )
    HazardAttempt.objects.bulk_create(
        HazardAttempt(
            student=student,
            clip_id=clip["id"],
            score=clip["score"],
            top_score=clip["topScore"],
            voided=clip["voided"],
            void_reason=clip.get("voidReason") or "",
        )
        for clip in result["clips"]
    )
    notifications.queue_streak_check(student)
    return attempt


def mark_lesson_done(student, subchapter: Subchapter) -> None:
    """Record that a student finished a subchapter. Finishing it twice changes nothing."""
    LessonProgress.objects.get_or_create(student=student, subchapter=subchapter)
    notifications.queue_streak_check(student)


def mark_section_read(student, section: EbookSection) -> None:
    """Record that a student read a section of a book. Reading it twice changes nothing."""
    EbookSectionProgress.objects.get_or_create(student=student, section=section)
    notifications.queue_streak_check(student)


FINISHED_WITHIN_SECONDS = Decimal("1.5")
MAX_VIDEO_SECONDS = Decimal("999999.99")
SECONDS = Decimal("0.01")


def _seconds(value) -> Decimal | None:
    if value is None:
        return None
    seconds = min(max(Decimal(str(value)), Decimal("0.00")), MAX_VIDEO_SECONDS)
    return seconds.quantize(SECONDS, rounding=ROUND_DOWN)


def live_video(slug: str, at=None):
    at = at or timezone.now()
    contents = live(LearningContent.objects.select_related("subchapter__chapter", "media"), at).filter(
        subchapter__in=live(Subchapter.objects.all(), at),
        subchapter__chapter__in=live(Chapter.objects.all(), at),
    )
    return contents.filter(slug=slug, content_type=ContentType.VIDEO, media__isnull=False).first()


def save_video_position(*, student, content: LearningContent, position_seconds, duration_seconds=None):
    position = _seconds(position_seconds) or Decimal("0.00")

    with transaction.atomic():
        known = (
            VideoProgress.objects.filter(student=student, content=content)
            .values_list("duration_seconds", flat=True)
            .first()
        )
        duration = _seconds(duration_seconds) or known
        if duration:
            position = min(position, duration)

        progress, _ = VideoProgress.objects.update_or_create(
            student=student,
            content=content,
            defaults={"position_seconds": position, "duration_seconds": duration},
        )
    return progress


def timecode(seconds) -> str:
    total = int(Decimal(str(seconds or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, rest = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{rest:02d}"
    return f"{minutes:02d}:{rest:02d}"


def video_position_of(progress) -> dict:
    if progress is None:
        return {
            "positionSeconds": 0.0,
            "durationSeconds": None,
            "resumeAt": timecode(0),
            "hasProgress": False,
            "finished": False,
            "updatedAt": None,
        }

    duration = progress.duration_seconds
    finished = (
        duration is not None
        and progress.position_seconds > 0
        and progress.position_seconds >= duration - FINISHED_WITHIN_SECONDS
    )
    return {
        "positionSeconds": float(progress.position_seconds),
        "durationSeconds": float(duration) if duration is not None else None,
        "resumeAt": timecode(progress.position_seconds),
        "hasProgress": True,
        "finished": bool(finished),
        "updatedAt": progress.updated_at,
    }


def video_positions_for(student, contents) -> dict[int, dict]:
    ids = [content.pk for content in contents if content.content_type == ContentType.VIDEO]
    if not ids:
        return {}

    saved = VideoProgress.objects.filter(student=student, content_id__in=ids)
    return {progress.content_id: video_position_of(progress) for progress in saved}


def resumable_videos_by_lesson(student, subchapters) -> dict[int, dict]:
    if not subchapters:
        return {}

    saved = (
        VideoProgress.objects.filter(
            student=student,
            content__in=live(LearningContent.objects.filter(content_type=ContentType.VIDEO)),
            content__subchapter__in=subchapters,
        )
        .select_related("content")
        .order_by("-updated_at", "-pk")
    )

    resumable: dict[int, dict] = {}
    for progress in saved:
        resumable.setdefault(
            progress.content.subchapter_id, {"slug": progress.content.slug, **video_position_of(progress)}
        )
    return resumable


def sections_read_by(student) -> set[int]:
    """Which e-book sections this student has read, for marking up a contents list in one query."""
    return set(EbookSectionProgress.objects.filter(student=student).values_list("section_id", flat=True))


def completed_lesson_ids(student) -> set[int]:
    """Which subchapters this student has finished, for marking up a listing in one query."""
    return set(LessonProgress.objects.filter(student=student).values_list("subchapter_id", flat=True))


def saved_question_keys(student) -> list[str]:
    return list(SavedQuestion.objects.filter(student=student).values_list("question__key", flat=True))


def attempts_on_live_questions(student, at=None):
    return QuestionAttempt.objects.filter(student=student, question__in=live(Question.objects.all(), at))


def mastery_by_chapter(student, chapters) -> dict[int, dict]:
    """:func:`mastery_of` for several chapters at once, in two queries instead of two per chapter.

    Listing the whole course asked the database twice per chapter, which is most of what made
    that page slow — the queries are each tiny, but the database is on the other side of the
    internet, so the round trips are the cost.

    Returns the same ``{"mastery": int, "questions": int}`` shape as :func:`mastery_of`, keyed by
    chapter id, with an entry for every chapter asked about including those with no questions.
    """
    chapter_ids = [chapter.pk for chapter in chapters]
    if not chapter_ids:
        return {}

    totals = dict(
        live(Question.objects.filter(chapter_id__in=chapter_ids)).values_list("chapter_id").annotate(total=Count("id"))
    )

    # Distinct because a question answered correctly five times is one question learnt, which is
    # the whole point of counting questions rather than attempts.
    learnt = dict(
        attempts_on_live_questions(student)
        .filter(was_correct=True, question__chapter_id__in=chapter_ids)
        .values_list("question__chapter_id")
        .annotate(learnt=Count("question_id", distinct=True))
    )

    return {chapter_id: _mastery(learnt.get(chapter_id, 0), totals.get(chapter_id, 0)) for chapter_id in chapter_ids}


def _mastery(learnt: int, total: int) -> dict:
    if not total:
        return {"mastery": 0, "questions": 0}
    return {"mastery": round(learnt * 100 / total), "questions": total}


def mastery_of(student, chapter: Chapter) -> dict:
    """How much of a chapter's question bank the student has got right at least once.

    Counted over questions rather than attempts, so answering the same question ten times does
    not look like progress, and a question answered right once stays counted even if a later
    attempt was wrong — this is "have you learnt it", not "how are you doing today".
    """
    total = live(Question.objects.filter(chapter=chapter)).count()
    if not total:
        return {"mastery": 0, "questions": 0}

    learnt = (
        attempts_on_live_questions(student)
        .filter(was_correct=True, question__chapter=chapter)
        .values("question_id")
        .distinct()
        .count()
    )
    return _mastery(learnt, total)


def study_days(student) -> list:
    """The distinct dates this student did something, newest first.

    Answering a question, finishing a lesson and reading a section of a book all count, so a day
    spent reading is not lost.
    """
    answered = QuestionAttempt.objects.filter(student=student).dates("created_at", "day", order="DESC")
    read = LessonProgress.objects.filter(student=student).dates("completed_at", "day", order="DESC")
    booked = EbookSectionProgress.objects.filter(student=student).dates("read_at", "day", order="DESC")
    return sorted(set(answered) | set(read) | set(booked), reverse=True)


def streak(student, today=None, days=None) -> int:
    """How many days in a row up to today the student has studied.

    Today not being used yet does not break the streak — it is only broken by a day that has
    passed with nothing in it — so the number does not drop to zero overnight.
    """
    today = today or timezone.localdate()
    days = study_days(student) if days is None else days
    if not days:
        return 0

    first = days[0]
    if (today - first).days > 1:
        return 0

    count = 1
    for earlier in days[1:]:
        if (first - earlier).days != 1:
            break
        count += 1
        first = earlier
    return count


def question_ids_attempted(student):
    """Every question this student has attempted, right or wrong, for going back over."""
    return QuestionAttempt.objects.filter(student=student).values("question_id")


def question_ids_already_learnt(student):
    """Questions this student has answered correctly at least once.

    Practising "weak" areas excludes these, which leaves both the questions got wrong and the
    ones never seen — so a new student asking for their weak areas gets the whole bank rather
    than nothing.
    """
    return QuestionAttempt.objects.filter(student=student, was_correct=True).values_list("question_id", flat=True)


def question_ids_answered_wrong(student):
    """Questions the student's most recent attempt got wrong, for practising mistakes.

    It is the latest attempt that decides, not whether the question was ever got right: a
    question put right and then got wrong again is a mistake once more, and one got wrong and
    then put right is no longer a mistake to go back over.
    """
    latest = (
        QuestionAttempt.objects.filter(student=student)
        .order_by("question_id", "-created_at", "-id")
        .distinct("question_id")
        .values_list("question_id", "was_correct")
    )
    return {question_id for question_id, was_correct in latest if not was_correct}


def best_practice_result(attempts) -> dict:
    best = max(
        ((percent_of(score, total), total, score) for score, total in attempts.values_list("score", "total")),
        default=None,
    )
    if best is None:
        return {"bestPracticeScore": None, "bestPracticeCorrect": None, "bestPracticeTotal": None}

    percent, total, score = best
    return {"bestPracticeScore": percent, "bestPracticeCorrect": score, "bestPracticeTotal": total}


def progress_summary(student) -> dict:
    """The headline numbers for the Today screen."""
    days = study_days(student)
    attempts = attempts_on_live_questions(student)
    answered = attempts.values("question_id").distinct().count()
    learnt = attempts.filter(was_correct=True).values("question_id").distinct().count()
    bank = live(Question.objects.all()).count()
    sittings = MockAttempt.objects.filter(student=student)
    mocks = sittings.filter(exam__kind=ExamKind.MOCK)
    practices = sittings.filter(exam__kind=ExamKind.PRACTICE)
    best_mock = mocks.order_by("-score").first()
    clips = HazardAttempt.objects.filter(student=student)
    best_clip = clips.order_by("-score").first()

    return {
        # The days are already in hand, so the streak does not go and fetch them again.
        "streak": streak(student, days=days),
        "studyDays": len(days),
        "lastStudiedOn": days[0] if days else None,
        "questionsAnswered": answered,
        "questionsLearnt": learnt,
        "mastery": round(learnt * 100 / bank) if bank else 0,
        "lessonsCompleted": LessonProgress.objects.filter(student=student).count(),
        "sectionsRead": EbookSectionProgress.objects.filter(student=student).count(),
        "mockAttempts": mocks.count(),
        "mocksPassed": mocks.filter(passed=True).count(),
        "bestMockScore": best_mock.score if best_mock else None,
        "practiceAttempts": practices.count(),
        "practiceExamsPassed": practices.filter(passed=True).count(),
        **best_practice_result(practices),
        "hazardAttempts": clips.count(),
        "bestHazardScore": best_clip.score if best_clip else None,
    }


# --- Hazard perception ---------------------------------------------------------------------------


# A clip this student has never attempted, as their statistics read.
NO_ATTEMPTS_YET = {"attempts": 0, "bestScore": None}


def hazard_stats_for(student, clips) -> dict[int, dict]:
    stats = {clip.pk: dict(NO_ATTEMPTS_YET) for clip in clips}
    if not stats:
        return stats

    rows = (
        HazardAttempt.objects.filter(student=student, clip__in=stats)
        .values_list("clip_id")
        .annotate(attempts=Count("id"), best=Max("score"))
    )
    for clip_id, attempts, best in rows:
        stats[clip_id] = {"attempts": attempts, "bestScore": best}
    return stats


def student_view_of_clip(clip: HazardClip, stats: dict | None = None) -> dict:
    """A clip as a student is given it, before they watch.

    Deliberately without the hazard timings: sending them would tell the student exactly when to
    click, which is the entire thing being tested. Only the clip's own scoring happens here, and
    only after they have committed to their clicks.
    """
    view = {
        "slug": clip.slug,
        "title": clip.title,
        "description": clip.description,
        "url": clip.media.file.url,
        "subtitles": subtitles_of(clip.media),
        "thumbnailUrl": clip.thumbnail_url,
        "hazards": clip.windows.count(),
        "topScore": clip.top_score,
        "maxClicks": clip.max_clicks,
    }
    if stats is not None:
        view |= {"attempts": stats["attempts"], "bestScore": stats["bestScore"]}
    return view


def score_clip(clip: HazardClip, clicked_at: list[float]) -> dict:
    """Score one attempt at a clip.

    Each hazard is scored by the best click that fell inside its window, so a student who clicks
    twice at the same hazard is neither rewarded nor punished for the second click. Clicking more
    than the clip allows scores nothing at all, which is what stops clicking throughout from
    passing as perception.
    """
    windows = list(clip.windows.all())
    verdict = hazard_detection.inspect(
        clicked_at, windows, max_clicks=clip.max_clicks, duration_seconds=clip.duration_seconds
    )
    voided = verdict.voided

    hazards: list[dict] = []
    score = 0
    for window in windows:
        best = 0 if voided else max((window.score_for(moment) for moment in verdict.scorable), default=0)
        score += best
        hazards.append(
            {
                "label": window.label,
                "startsAt": float(window.starts_at),
                "endsAt": float(window.ends_at),
                "score": best,
                "spotted": best > 0,
            }
        )

    return {
        "score": score,
        "topScore": clip.top_score,
        "voided": voided,
        "voidReason": verdict.reason or None,
        # Revealed now the attempt is over, so the student can see what they missed and when.
        "hazards": hazards,
    }


def record_hazard_attempt(student, clip: HazardClip, result: dict) -> None:
    """Remember one go at a clip, with the score it earned."""
    HazardAttempt.objects.create(
        student=student,
        clip=clip,
        score=result["score"],
        top_score=result["topScore"],
        voided=result["voided"],
        void_reason=result.get("voidReason") or "",
    )


def hazard_sheet_for(clip: HazardClip) -> dict:
    """A clip with its hazard timings, for staff checking them before publishing.

    A separate function from :func:`student_view_of_clip`, which never carries the timings, so a
    page meant for staff cannot be wired to students by mistake.
    """
    return {
        "url": clip.media.file.url,
        "max_clicks": clip.max_clicks,
        "top_score": clip.top_score,
        "windows": [
            {
                "label": window.label,
                "starts_at": float(window.starts_at),
                "ends_at": float(window.ends_at),
            }
            for window in clip.windows.all()
        ],
    }


def live_ebooks(at=None):
    """The e-books students can see now, in the order staff put them in."""
    return live(Ebook.objects.select_related("file"), at=at).order_by("order", "title")


def ebook_sections(ebook: Ebook, at=None):
    """The book's live sections, in the order staff put them in."""
    return live(ebook.sections.select_related("media").prefetch_related("media__subtitles"), at=at).order_by(
        "order", "title"
    )


def ebook_section(slug: str, at=None):
    return (
        live(EbookSection.objects.select_related("ebook", "media").prefetch_related("media__subtitles"), at=at)
        .filter(slug=slug, ebook__in=live_ebooks(at))
        .first()
    )


def blocks_of_section(section: EbookSection) -> list[dict]:
    blocks: list[dict] = []

    for piece in html.split_on_signs(section.body_html):
        if "html" in piece:
            blocks.append(
                {
                    "type": "html",
                    "title": "",
                    "html": resolved_media_in(piece["html"]),
                }
            )
            continue

        drawn = signs_by_code(piece["codes"])
        if drawn:
            blocks.append({"type": "signs", "caption": piece["caption"], "signs": drawn})

    document = section.media
    if document is not None and MediaKind(document.kind) in EBOOK_MEDIA_KINDS:
        block: dict = {
            "type": EBOOK_MEDIA_KINDS[MediaKind(document.kind)],
            "title": "",
            "url": document.file.url,
        }
        if MediaKind(document.kind) == MediaKind.VIDEO:
            block["subtitles"] = subtitles_of(document)
        blocks.append(block)

    return blocks


def signs_by_code(codes: list[str]) -> list[dict]:
    if not codes:
        return []

    found = {
        sign.code: {
            "code": sign.code,
            "name": sign.name,
            "category": sign.category,
            "meaning": sign.meaning,
            "spec": sign.spec,
        }
        for sign in Sign.objects.filter(code__in=codes)
    }
    return [found[code] for code in codes if code in found]


def unknown_sign_codes(body_html: str | None) -> list[str]:
    """The codes in this text that name no sign, so staff are told before students are."""
    asked = html.sign_codes_in(body_html)
    if not asked:
        return []

    known = set(Sign.objects.filter(code__in=asked).values_list("code", flat=True))
    return list(dict.fromkeys(code for code in asked if code not in known))


def section_in_the_contents(section: EbookSection, number: int, read: set[int]) -> dict:
    """One section as the contents list it: enough to show and choose it, not to read it."""
    return {
        "slug": section.slug,
        "title": section.title,
        "summary": section.summary,
        "number": number,
        "read": section.pk in read,
    }


def contents_of(sections, read: set[int]) -> list[dict]:
    """A book's sections numbered from 1, each marked read or not for this student."""
    return [section_in_the_contents(section, number, read) for number, section in enumerate(sections, start=1)]


def _ebook_basics(ebook: Ebook) -> dict:
    return {
        "slug": ebook.slug,
        "title": ebook.title,
        "description": ebook.description,
        "coverUrl": ebook.cover_url,
        "updatedAt": ebook.updated_at,
    }


def ebook_on_the_shelf(ebook: Ebook) -> dict:
    return {**_ebook_basics(ebook), "hasDocument": ebook.file_id is not None}


def ebook_sheet_for(ebook: Ebook) -> dict:
    document = ebook.file if ebook.file_id else None
    return {
        "url": document.file.url if document and document.file else "",
        "file_title": document.title if document else "",
        "kind": document.get_kind_display() if document else "",
        "content_type": document.content_type if document else "",
        "size_bytes": document.size_bytes if document else 0,
        "cover_url": ebook.cover_url,
        "sections": contents_of(ebook_sections(ebook), read=set()),
    }


def student_view_of_ebook(ebook: Ebook) -> dict:
    """An e-book to read, with the link to its file when it has one.

    The URL is signed and expires, and is built at the moment of serving rather than stored, so a
    link cannot be kept or shared beyond its lifetime — the same arrangement as a lesson's video.
    """
    document = ebook.file if ebook.file_id else None
    return {
        **_ebook_basics(ebook),
        "url": document.file.url if document else None,
        "contentType": document.content_type if document else None,
        "sizeBytes": document.size_bytes if document else None,
    }
