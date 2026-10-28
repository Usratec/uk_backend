"""The learning material students work through.

One hierarchy holds everything::

    Chapter  →  Subchapter  →  LearningContent (theory, video, document, question, hazard)

Packages buy access to parts of it: a chapter, a subchapter or a single item can be limited to
the packages that include it, and a restriction on a chapter covers everything inside it.

A **chapter** is a major area such as "Road signs". A **subchapter** divides it into a sitting's
worth of study. A **learning content** item is the thing a student actually reads or does, and its
``content_type`` says which kind it is. Adding a kind of material later means another content type,
not another hierarchy, so the Highway Code e-book is simply a chapter whose subchapters hold
theory and a document, rather than a second structure beside this one.

Two things deliberately sit outside the hierarchy, because they are libraries rather than places
in a course: road **signs**, which the frontend draws from a specification, and — from a later
branch — the question bank and hazard clips, which are referenced by content rather than owned
by it.

Content is addressed by ``slug`` because that is what the API asks for, so a slug becomes part of
the public contract once published.
"""

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.utils.text import slugify

from apps.core.models import PublishableModel, TimestampedModel
from apps.learning import html


class Chapter(PublishableModel, TimestampedModel):
    """A major area of the course, such as "Road signs" or "The Highway Code"."""

    slug = models.SlugField(unique=True, help_text="Used in links. Changing it breaks saved links.")
    title = models.CharField(max_length=120)
    description = models.CharField(max_length=300, blank=True)
    icon = models.CharField(max_length=40, blank=True, help_text="Icon name the frontend knows.")
    order = models.PositiveSmallIntegerField(default=0, help_text="Lower numbers come first.")

    only_for_packages = models.ManyToManyField(
        "catalog.Package",
        blank=True,
        related_name="%(class)ss",
        verbose_name="only for packages",
        help_text="Leave empty to include it in every package.",
    )

    class Meta:
        ordering = ["order", "title"]
        permissions = [("publish_chapter", "Can publish and unpublish chapters")]

    def __str__(self) -> str:
        return self.title


class Subchapter(PublishableModel, TimestampedModel):
    """A section of a chapter, holding the material for one sitting of study."""

    chapter = models.ForeignKey(Chapter, on_delete=models.PROTECT, related_name="subchapters")
    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=160)
    summary = models.CharField(max_length=300, blank=True)
    minutes = models.PositiveSmallIntegerField(default=5, help_text="Rough study time, shown to students.")
    # Counted from 1 within its own chapter, so renumbering one chapter never disturbs another.
    order = models.PositiveSmallIntegerField(default=0, help_text="Position within this chapter.")

    only_for_packages = models.ManyToManyField(
        "catalog.Package",
        blank=True,
        related_name="%(class)ss",
        verbose_name="only for packages",
        help_text="Leave empty to include it in every package.",
    )

    class Meta:
        ordering = ["chapter", "order", "title"]
        indexes = [models.Index(fields=["chapter", "order"])]
        permissions = [("publish_subchapter", "Can publish and unpublish subchapters")]

    def __str__(self) -> str:
        return self.title


class ContentType(models.TextChoices):
    """What kind of material a content item holds.

    The hierarchy does not change when a kind is added: the item carries its type, and the kinds
    beyond theory arrive with the work that can actually serve them.
    """

    THEORY = "theory", "Theory"
    VIDEO = "video", "Video"
    DOCUMENT = "document", "Document"
    QUESTION = "question", "Question"
    HAZARD = "hazard", "Hazard clip"


# The kinds staff can create today. The rest are part of the shape, not yet of the product, and
# publishing one is refused rather than quietly serving an empty item.
AVAILABLE_CONTENT_TYPES = (
    ContentType.THEORY,
    ContentType.QUESTION,
    ContentType.VIDEO,
    ContentType.DOCUMENT,
    ContentType.HAZARD,
)


class LearningContent(PublishableModel, TimestampedModel):
    """One piece of material inside a subchapter."""

    subchapter = models.ForeignKey(Subchapter, on_delete=models.PROTECT, related_name="content")
    content_type = models.CharField(
        max_length=20,
        choices=ContentType.choices,
        default=ContentType.THEORY,
        help_text="What kind of material this is.",
    )
    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=160)
    body_html = models.TextField("content", blank=True, help_text="Used by theory material.")
    media = models.ForeignKey(
        "learning.MediaAsset",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="used_in",
        help_text="Used by video and document material: which file to show.",
    )
    hazard_clip = models.ForeignKey(
        "learning.HazardClip",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="used_in",
        help_text="Used by hazard material: which clip to show.",
    )
    question = models.ForeignKey(
        "learning.Question",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="used_in",
        help_text="Used by question material: which question from the bank to ask here.",
    )
    order = models.PositiveSmallIntegerField(default=0, help_text="Position within this subchapter.")

    only_for_packages = models.ManyToManyField(
        "catalog.Package",
        blank=True,
        related_name="%(class)ss",
        verbose_name="only for packages",
        help_text="Leave empty to include it in every package.",
    )

    class Meta:
        ordering = ["subchapter", "order", "title"]
        indexes = [models.Index(fields=["subchapter", "order"])]
        verbose_name = "learning content"
        verbose_name_plural = "learning content"
        permissions = [("publish_learningcontent", "Can publish and unpublish learning content")]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        # Cleaned here rather than in the form, so content written by an import or the shell is
        # as safe as content typed by staff.
        self.body_html = html.clean(self.body_html)
        super().save(*args, **kwargs)


class MediaKind(models.TextChoices):
    VIDEO = "video", "Video"
    DOCUMENT = "document", "Document"
    IMAGE = "image", "Image"


def media_path(instance, filename: str) -> str:
    """Where an upload is stored: a folder per kind, the name it was given kept.

    The storage backend refuses to overwrite, so two uploads with the same name become two
    objects rather than one replacing the other.
    """
    return f"learning/{instance.kind}/{filename}"


class MediaAsset(TimestampedModel):
    """A file staff uploaded — a video, a document, an image.

    A library rather than a place in the course, like signs: content refers to an asset, so the
    same video can be used in two lessons without being uploaded twice.

    The file itself lives in a private bucket and is served by a short-lived signed URL that the
    API issues only once it has checked the student may see the content referring to it. That is
    why nothing here is a public URL: a link that escaped would otherwise be permanent.
    """

    title = models.CharField(max_length=160, help_text="How staff find this again.")
    kind = models.CharField(max_length=10, choices=MediaKind.choices)
    file = models.FileField(upload_to=media_path)
    # Recorded at upload because asking the bucket costs a request, and these are shown in a list.
    size_bytes = models.PositiveBigIntegerField(default=0, editable=False)
    content_type = models.CharField(max_length=100, blank=True, editable=False)
    checksum = models.CharField(max_length=64, blank=True, editable=False, db_index=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="uploads"
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        # Taken from the upload itself rather than trusted from a form field.
        if self.file:
            self.size_bytes = getattr(self.file, "size", 0) or 0
            self.content_type = getattr(getattr(self.file, "file", None), "content_type", "") or self.content_type
        super().save(*args, **kwargs)


class SubtitleLanguage(models.TextChoices):
    EN_GB = "en-GB", "British English"
    EN_US = "en-US", "American English"
    FR_FR = "fr-FR", "French"
    DE_DE = "de-DE", "German"
    NL_NL = "nl-NL", "Dutch"


def default_subtitle_language() -> str:
    configured = (settings.LANGUAGE_CODE or "").lower()
    return next((code for code in SubtitleLanguage.values if code.lower() == configured), "")


def subtitle_path(instance, filename: str) -> str:
    return f"learning/subtitle/{filename}"


class Subtitle(TimestampedModel):
    asset = models.ForeignKey(
        MediaAsset,
        on_delete=models.CASCADE,
        related_name="subtitles",
        help_text="The video this track belongs to.",
    )
    language = models.CharField(
        max_length=16,
        choices=SubtitleLanguage.choices,
        help_text="The language of this track, as a BCP 47 tag.",
    )
    file = models.FileField(
        upload_to=subtitle_path,
        validators=[FileExtensionValidator(["vtt"])],
        help_text="A WebVTT (.vtt) file. Its contents are checked on upload, not just its name.",
    )

    class Meta:
        ordering = ["asset", "language"]
        constraints = [
            models.UniqueConstraint(fields=["asset", "language"], name="one_subtitle_per_language_per_asset")
        ]
        verbose_name = "subtitle track"

    def __str__(self) -> str:
        return f"{self.get_language_display()} subtitles for {self.asset_id and self.asset.title}"

    def clean(self) -> None:
        super().clean()
        if self.asset_id and self.asset.kind != MediaKind.VIDEO:
            raise ValidationError(f"'{self.asset.title}' is not a video, so it has nothing to subtitle.")


class SignCategory(models.TextChoices):
    WARNING = "warning", "Warning"
    REGULATORY = "regulatory", "Regulatory"
    INFORMATION = "information", "Information"
    MOTORWAY = "motorway", "Motorway"


class Sign(TimestampedModel):
    """A road sign, stored as instructions for drawing it rather than as an image.

    A library rather than a place in the course: content refers to signs by code, and the frontend
    draws them from its own symbol set, so they stay sharp at any size and need no uploads.
    """

    code = models.SlugField(unique=True, help_text="How content refers to this sign, e.g. give-way.")
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=20, choices=SignCategory.choices)
    meaning = models.CharField(max_length=300)
    spec = models.JSONField(default=dict, help_text="Shape, colours and symbol. See the drawing rules.")
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["category", "order", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.code})"


class QuestionType(models.TextChoices):
    """How a question is answered.

    ``image`` is a single-answer question whose options are pictures rather than words — either a
    road sign the frontend draws from its specification or an uploaded image — which the frontend
    lays out as a grid.
    """

    SINGLE = "single", "One answer"
    MULTI = "multi", "Several answers"
    IMAGE = "image", "Road sign answers"


class Question(PublishableModel, TimestampedModel):
    """A question in the bank.

    Questions live in one bank and are referred to wherever they are used — in a subchapter, in a
    practice exam, in a mock test — so the same question is never copied and an edit reaches every
    place at once.

    Which package a question belongs to follows from its chapter, so there is no separate gate
    here to fall out of step with the material it is testing.
    """

    key = models.SlugField(unique=True, help_text="How content and bookmarks refer to this question.")
    chapter = models.ForeignKey(
        Chapter,
        on_delete=models.PROTECT,
        related_name="questions",
        help_text="The area this question belongs to. Students practise and are scored by it.",
    )
    question_type = models.CharField("type", max_length=10, choices=QuestionType.choices, default=QuestionType.SINGLE)
    prompt = models.TextField(help_text="The question itself.")
    explanation = models.TextField(blank=True, help_text="Shown after answering.")
    media_sign = models.ForeignKey(
        Sign,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="questions",
        verbose_name="sign shown with the question",
    )
    media_image = models.ForeignKey(
        "learning.MediaAsset",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="questions",
        limit_choices_to={"kind": MediaKind.IMAGE},
        verbose_name="image shown with the question",
        help_text="A picture or GIF from the media library. Upload one below to add it to the library.",
    )
    learn_more = models.ForeignKey(
        Subchapter,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="questions",
        help_text="Where a student can go to read about this, offered after answering.",
    )

    class Meta:
        ordering = ["chapter", "key"]
        permissions = [("publish_question", "Can publish and unpublish questions")]

    def __str__(self) -> str:
        return self.prompt[:70]

    @property
    def correct_option_ids(self) -> list[str]:
        return [option.option_id for option in self.options.all() if option.is_correct]

    @property
    def pick(self) -> int:
        """How many answers a student marks, which is simply how many are right."""
        return len(self.correct_option_ids) or 1


class QuestionOption(TimestampedModel):
    """One answer a student can choose.

    Whether it is the right one is never serialised to students before they answer: the API builds
    their view of a question without this column.
    """

    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="options")
    option_id = models.CharField(
        max_length=4,
        help_text="Short id the frontend sends back, such as a, b, c.",
    )
    text = models.CharField(max_length=300, blank=True)
    sign = models.ForeignKey(
        Sign,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="answer_options",
        help_text="For questions answered with road signs.",
    )
    image = models.ForeignKey(
        "learning.MediaAsset",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="answer_options",
        limit_choices_to={"kind": MediaKind.IMAGE},
        help_text="For answers that are a picture rather than a sign drawn from a specification.",
    )
    is_correct = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["question", "order", "option_id"]
        constraints = [models.UniqueConstraint(fields=["question", "option_id"], name="unique_option_id_per_question")]

    def __str__(self) -> str:
        return f"{self.option_id}. {self.text or self.sign_id or ''}"


class ExamKind(models.TextChoices):
    PRACTICE = "practice", "Practice exam"
    MOCK = "mock", "Mock test"


class PracticeExam(PublishableModel, TimestampedModel):
    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=160)
    description = models.CharField(max_length=300, blank=True)
    kind = models.CharField(max_length=10, choices=ExamKind.choices, default=ExamKind.PRACTICE)

    time_limit_seconds = models.PositiveIntegerField(null=True, blank=True, help_text="Leave empty for no time limit.")
    pass_mark = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        help_text="How many questions must be right to pass. Leave empty on a paper of clips alone.",
    )
    hazard_pass_mark = models.PositiveSmallIntegerField(
        null=True,
        blank=True,
        verbose_name="hazard perception pass mark",
        help_text="How many hazard marks are needed to pass the clips. Leave empty until it is decided.",
    )
    order = models.PositiveSmallIntegerField(default=0)

    questions = models.ManyToManyField(Question, through="learning.ExamQuestion", related_name="exams")
    hazard_clips = models.ManyToManyField(
        "learning.HazardClip",
        through="learning.ExamHazardClip",
        related_name="exams",
        blank=True,
        verbose_name="hazard clips",
        help_text="Clips sat after the questions. Leave empty for a question-only paper.",
    )

    class Meta:
        ordering = ["order", "title"]
        permissions = [("publish_practiceexam", "Can publish and unpublish exams")]

    def __str__(self) -> str:
        return self.title


class ExamQuestion(models.Model):
    """One question's place in an exam."""

    exam = models.ForeignKey(PracticeExam, on_delete=models.CASCADE, related_name="exam_questions")
    question = models.ForeignKey(Question, on_delete=models.PROTECT, related_name="exam_places")
    order = models.PositiveSmallIntegerField(default=0, help_text="Position within this exam.")

    class Meta:
        ordering = ["exam", "order"]
        constraints = [models.UniqueConstraint(fields=["exam", "question"], name="unique_question_per_exam")]

    def __str__(self) -> str:
        return f"{self.exam.title}: {self.question}"


class ExamHazardClip(models.Model):
    exam = models.ForeignKey(PracticeExam, on_delete=models.CASCADE, related_name="exam_clips")
    clip = models.ForeignKey("learning.HazardClip", on_delete=models.PROTECT, related_name="exam_places")
    order = models.PositiveSmallIntegerField(default=0, help_text="Position within this exam's clips.")

    class Meta:
        ordering = ["exam", "order"]
        verbose_name = "hazard clip in an exam"
        verbose_name_plural = "hazard clips in an exam"
        constraints = [models.UniqueConstraint(fields=["exam", "clip"], name="unique_clip_per_exam")]

    def __str__(self) -> str:
        return f"{self.exam.title}: {self.clip}"


# --- What a student has done ---------------------------------------------------------------------
#
# These are records of a person, not of the course, so they are never edited by staff. Each one is
# written by the endpoint the student was already calling, so the frontend needs no extra request
# to keep progress up to date. Streaks and mastery are worked out from these rather than stored,
# so a recount can never disagree with the attempts behind it.


class QuestionAttempt(TimestampedModel):
    """One answer a student gave. Kept per attempt, not per question.

    Keeping every attempt is what makes "questions I got wrong" and "questions I keep getting
    wrong" answerable later; a single row per question would lose that the moment they retried.
    """

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="question_attempts")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="attempts")
    was_correct = models.BooleanField()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["student", "question", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.student} · {self.question.key} · {'right' if self.was_correct else 'wrong'}"


class MockAttempt(TimestampedModel):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mock_attempts")
    exam = models.ForeignKey(PracticeExam, on_delete=models.CASCADE, related_name="attempts")
    score = models.PositiveSmallIntegerField(help_text="Questions answered correctly.")
    total = models.PositiveSmallIntegerField(help_text="Questions asked.")
    passed = models.BooleanField()
    unanswered = models.PositiveSmallIntegerField(default=0, help_text="Questions left blank.")
    hazard_score = models.PositiveSmallIntegerField(default=0, help_text="Scored on this sitting's hazard clips.")
    hazard_top_score = models.PositiveSmallIntegerField(
        default=0, help_text="The most this sitting's clips could score. 0 for a question-only paper."
    )
    time_taken_seconds = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text=(
            "How long the questions took, from starting the exam to ending it or the timer running "
            "out. The clips are not counted. Empty when the sitting was not timed."
        ),
    )
    result = models.JSONField(
        default=dict,
        blank=True,
        editable=False,
        help_text="The marked paper as it was reported, so the student can open it again.",
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["student", "exam"])]

    def __str__(self) -> str:
        return f"{self.student} · {self.exam.title} · {self.score}/{self.total}"

    @property
    def incorrect(self) -> int:
        """Questions answered wrongly, as opposed to not answered at all."""
        return max(self.total - self.score - self.unanswered, 0)


class LessonProgress(models.Model):
    """That a student has finished a subchapter.

    One model covers both a lesson being completed and an e-book section being read, because the
    e-book is a chapter of subchapters like any other — so there is nothing to keep in step.
    """

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="lesson_progress")
    subchapter = models.ForeignKey(Subchapter, on_delete=models.CASCADE, related_name="progress")
    completed_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-completed_at"]
        # Django would otherwise pluralise "lesson progress" as "lesson progresss".
        verbose_name = "lesson progress"
        verbose_name_plural = "lesson progress"
        constraints = [models.UniqueConstraint(fields=["student", "subchapter"], name="one_progress_row_per_lesson")]

    def __str__(self) -> str:
        return f"{self.student} · {self.subchapter.title}"


class VideoProgress(TimestampedModel):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="video_progress")
    content = models.ForeignKey(LearningContent, on_delete=models.CASCADE, related_name="video_progress")
    position_seconds = models.DecimalField(
        max_digits=9,
        decimal_places=2,
        validators=[MinValueValidator(0)],
        help_text="How far into the video the student had watched.",
    )
    duration_seconds = models.DecimalField(
        max_digits=9,
        decimal_places=2,
        null=True,
        blank=True,
        validators=[MinValueValidator(0)],
        help_text="How long the video is, as the player reported it. Empty when it never said.",
    )

    class Meta:
        ordering = ["-updated_at"]
        verbose_name = "video progress"
        verbose_name_plural = "video progress"
        constraints = [
            models.UniqueConstraint(fields=["student", "content"], name="one_video_position_per_lesson_video"),
            models.CheckConstraint(condition=models.Q(position_seconds__gte=0), name="video_position_is_not_negative"),
            models.CheckConstraint(
                condition=models.Q(duration_seconds__isnull=True) | models.Q(duration_seconds__gte=0),
                name="video_duration_is_not_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.student} · {self.content.title} · {self.position_seconds}s"


class SavedQuestion(models.Model):
    """A question a student put aside to come back to."""

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="saved_questions")
    question = models.ForeignKey(Question, on_delete=models.CASCADE, related_name="saved_by")
    saved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-saved_at"]
        constraints = [models.UniqueConstraint(fields=["student", "question"], name="one_saved_row_per_question")]

    def __str__(self) -> str:
        return f"{self.student} · {self.question.key}"


# --- Hazard perception ---------------------------------------------------------------------------
#
# A hazard clip is a piece of video with one or more moments in it where a hazard develops. The
# student clicks when they spot one, and the score depends on how early in that moment they
# clicked: the real test divides each hazard into five bands worth 5 down to 1.
#
# Staff give each hazard a single start and end, and the bands are worked out by dividing that
# window into five. One pair of numbers per hazard is enough to describe it and is far easier to
# get right than five, which matters because a mistimed window is invisible until students
# complain about it.


def hazard_thumbnail_path(instance, filename: str) -> str:
    return f"learning/hazard-thumbnail/{filename}"


class HazardClip(PublishableModel, TimestampedModel):
    """A video with hazards in it for a student to spot.

    The video comes from the media library like any other file, so a clip is the hazards plus a
    reference — nothing is uploaded twice.
    """

    slug = models.SlugField(unique=True)
    title = models.CharField(max_length=160)
    description = models.CharField(max_length=300, blank=True)
    media = models.ForeignKey(
        "learning.MediaAsset",
        on_delete=models.PROTECT,
        related_name="hazard_clips",
        help_text="The video from the media library.",
    )
    thumbnail = models.ImageField(
        upload_to=hazard_thumbnail_path,
        blank=True,
        help_text="A still shown before the clip plays. Optional — without one the player shows the video itself.",
    )
    # The real test ends an attempt that is clicked through indiscriminately, because clicking
    # constantly would otherwise score full marks without spotting anything.
    max_clicks = models.PositiveSmallIntegerField(
        default=15,
        help_text="Clicking more often than this scores nothing for the clip, as in the real test.",
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["order", "title"]
        permissions = [("publish_hazardclip", "Can publish and unpublish hazard clips")]

    def __str__(self) -> str:
        return self.title

    @property
    def top_score(self) -> int:
        """The most a student could score: five per hazard."""
        return self.windows.count() * HazardWindow.BANDS

    @property
    def thumbnail_url(self) -> str | None:
        return self.thumbnail.url if self.thumbnail else None


class HazardWindow(models.Model):
    """When one hazard is developing, and so when a click counts.

    The window is split into five equal bands. A click in the first band scores 5, the next 4, and
    so on to 1 in the last; a click outside every window scores nothing. Spotting a hazard early
    is the whole skill being tested, which is why the earliest band is worth the most.
    """

    #: How many score bands a window is divided into. Five, as in the real test.
    BANDS = 5

    clip = models.ForeignKey(HazardClip, on_delete=models.CASCADE, related_name="windows")
    label = models.CharField(max_length=120, help_text="What the hazard is, for staff. Never shown to students.")
    starts_at = models.DecimalField(
        max_digits=6, decimal_places=2, help_text="Seconds into the clip where the hazard starts developing."
    )
    ends_at = models.DecimalField(
        max_digits=6, decimal_places=2, help_text="Seconds into the clip by which it should have been spotted."
    )
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["clip", "starts_at"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(ends_at__gt=models.F("starts_at")), name="hazard_window_ends_after_it_starts"
            )
        ]

    def __str__(self) -> str:
        return f"{self.label} ({self.starts_at}–{self.ends_at}s)"

    def score_for(self, clicked_at: float) -> int:
        """What a click at this moment is worth: 5 in the first fifth of the window, down to 1."""
        start, end = float(self.starts_at), float(self.ends_at)
        if not start <= clicked_at <= end:
            return 0

        band_length = (end - start) / self.BANDS
        band = int((clicked_at - start) / band_length)
        # A click exactly on the end lands one band past the last one.
        return self.BANDS - min(band, self.BANDS - 1)


class HazardAttempt(TimestampedModel):
    """One go at a clip, and what it scored.

    The score is stored rather than recomputed, because retiming a hazard afterwards should not
    change what a student already achieved.
    """

    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="hazard_attempts")
    clip = models.ForeignKey(HazardClip, on_delete=models.CASCADE, related_name="attempts")
    score = models.PositiveSmallIntegerField()
    top_score = models.PositiveSmallIntegerField(help_text="The most that was available at the time.")
    voided = models.BooleanField(default=False, help_text="True when the clip was clicked through too often.")

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["student", "clip"])]

    def __str__(self) -> str:
        return f"{self.student} · {self.clip.title} · {self.score}/{self.top_score}"


def ebook_cover_path(instance, filename: str) -> str:
    return f"learning/ebook-cover/{filename}"


class Ebook(PublishableModel, TimestampedModel):
    slug = models.SlugField(
        unique=True,
        blank=True,
        help_text="Used in links. Filled in from the title if left empty. Changing it breaks saved links.",
    )
    title = models.CharField(max_length=160)
    description = models.CharField(max_length=300, blank=True, help_text="A sentence or two, shown on the shelf.")
    file = models.ForeignKey(
        "learning.MediaAsset",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ebooks",
        help_text="The document from the media library. Optional for a book that is read section by section.",
    )
    cover = models.ImageField(
        upload_to=ebook_cover_path,
        blank=True,
        help_text="The cover shown on the shelf. Optional.",
    )
    order = models.PositiveSmallIntegerField(default=0, help_text="Lower numbers come first.")

    class Meta:
        ordering = ["order", "title"]
        verbose_name = "ebook"
        verbose_name_plural = "ebooks"
        permissions = [("publish_ebook", "Can publish and unpublish ebooks")]

    def __str__(self) -> str:
        return self.title

    @property
    def cover_url(self) -> str | None:
        """The cover's URL, or None without one. Signed and short-lived, like every upload."""
        return self.cover.url if self.cover else None

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unused_ebook_slug(slugify(self.title), exclude=self.pk)
        super().save(*args, **kwargs)


class EbookSection(PublishableModel, TimestampedModel):
    ebook = models.ForeignKey(Ebook, on_delete=models.PROTECT, related_name="sections")
    slug = models.SlugField(unique=True, help_text="Used in links. Changing it breaks saved links.")
    title = models.CharField(max_length=160)
    summary = models.CharField(max_length=300, blank=True, help_text="A line shown in the contents.")
    body_html = models.TextField("content", blank=True, help_text="The section's text.")
    media = models.ForeignKey(
        "learning.MediaAsset",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ebook_sections",
        help_text="A video or document from the media library, shown after the text. Optional.",
    )
    order = models.PositiveSmallIntegerField(default=0, help_text="Position within this book.")

    class Meta:
        ordering = ["ebook", "order", "title"]
        indexes = [models.Index(fields=["ebook", "order"])]
        verbose_name = "ebook section"
        verbose_name_plural = "ebook sections"
        permissions = [("publish_ebooksection", "Can publish and unpublish ebook sections")]

    def __str__(self) -> str:
        return self.title

    def save(self, *args, **kwargs):
        self.body_html = html.clean(self.body_html)
        super().save(*args, **kwargs)


class EbookSectionProgress(models.Model):
    student = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ebook_progress")
    section = models.ForeignKey(EbookSection, on_delete=models.CASCADE, related_name="progress")
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-read_at"]
        verbose_name = "ebook reading progress"
        verbose_name_plural = "ebook reading progress"
        constraints = [models.UniqueConstraint(fields=["student", "section"], name="one_progress_row_per_section")]

    def __str__(self) -> str:
        return f"{self.student} · {self.section.title}"


def _unused_ebook_slug(base: str, exclude=None) -> str:
    base = base or "ebook"
    taken = Ebook.objects.exclude(pk=exclude) if exclude else Ebook.objects.all()
    candidate, suffix = base, 1
    while taken.filter(slug=candidate).exists():
        suffix += 1
        candidate = f"{base}-{suffix}"
    return candidate
