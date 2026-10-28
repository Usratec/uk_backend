"""The shapes the learning endpoints return, declared so Swagger can describe them.

These serializers are **documentation, not machinery**. The endpoints build their responses from
``services``, because what a student may see is decided by publishing state and package rather
than by field lists, and routing that through a serializer would hide the decision. So nothing
here is used to render a response — each one is named in an ``extend_schema`` so a developer
reading Swagger can see exactly what comes back.

Nothing enforces the match, so these have to be kept in step by hand: a key added to a response in
``api.py`` is added here too, and one removed is removed here.

Keys are camelCase where the endpoint returns camelCase: these describe the contract the frontend
already implements, not Python conventions.
"""

import math

from drf_spectacular.utils import PolymorphicProxySerializer, extend_schema_field
from rest_framework import serializers

from apps.learning.models import ExamKind, HazardVoidReason, QuestionType, SignCategory, SubtitleLanguage

MAX_CLICKS_PER_SUBMISSION = 500


class ClickTimeField(serializers.FloatField):
    def to_internal_value(self, data) -> float:
        moment = super().to_internal_value(data)
        if not math.isfinite(moment):
            self.fail("invalid")
        return moment


def click_times(**kwargs) -> serializers.ListField:
    return serializers.ListField(child=ClickTimeField(min_value=0), max_length=MAX_CLICKS_PER_SUBMISSION, **kwargs)


class SignSerializer(serializers.Serializer):
    """A road sign, which the frontend draws rather than loading an image."""

    code = serializers.SlugField(help_text="How content and questions refer to this sign.")
    name = serializers.CharField()
    category = serializers.ChoiceField(choices=SignCategory.choices)
    meaning = serializers.CharField()
    spec = serializers.DictField(
        help_text="Drawing instructions — shape, colours and symbol. The keys depend on the shape."
    )


class SignMapField(serializers.DictField):
    """The signs a response mentions, keyed by sign code."""

    def __init__(self, **kwargs):
        kwargs.setdefault("child", SignSerializer())
        kwargs.setdefault(
            "help_text", "Every sign this response refers to, keyed by code, so no second request is needed."
        )
        super().__init__(**kwargs)


# --- Questions -----------------------------------------------------------------------------------


class QuestionMediaSerializer(serializers.Serializer):
    """What is shown alongside a question. Only road signs so far."""

    kind = serializers.ChoiceField(choices=[("sign", "sign"), ("image", "image")])
    code = serializers.SlugField(required=False, help_text="With a sign: look it up in the response's `signs` map.")
    url = serializers.URLField(
        required=False,
        help_text="With a picture: a short-lived signed URL for the image or GIF.",
    )


class QuestionOptionSerializer(serializers.Serializer):
    """One answer to choose between.

    Whether it is the right one is deliberately absent: the answer is not in the payload at all,
    so it cannot be read out of the page. Send the id to ``/api/learn/answer/`` to find out.
    """

    id = serializers.CharField(help_text="Send this back to answer, e.g. 'a'.")
    text = serializers.CharField(required=False, help_text="Absent on answers that are a picture rather than words.")
    sign = serializers.SlugField(required=False, help_text="Present when the answer is a road sign.")
    image = serializers.URLField(
        required=False,
        help_text="Present when the answer is an uploaded picture: a short-lived signed URL.",
    )


class StudentQuestionSerializer(serializers.Serializer):
    """A question as a student is given it, before answering."""

    id = serializers.IntegerField(help_text="Send this as `questionId` when answering.")
    key = serializers.SlugField(help_text="How lesson blocks refer to this question.")
    topic = serializers.SlugField(help_text="The slug of the chapter it belongs to.")
    type = serializers.ChoiceField(choices=QuestionType.choices)
    prompt = serializers.CharField()
    media = QuestionMediaSerializer(allow_null=True)
    options = QuestionOptionSerializer(many=True)
    pick = serializers.IntegerField(help_text="How many answers the student should choose.")


class QuestionMapField(serializers.DictField):
    """The questions a response's check blocks name, keyed by question key."""

    def __init__(self, **kwargs):
        kwargs.setdefault("child", StudentQuestionSerializer())
        kwargs.setdefault("help_text", "The questions this content asks, keyed by the key its check blocks name.")
        super().__init__(**kwargs)


# --- Content blocks ------------------------------------------------------------------------------


class HazardBlockSerializer(serializers.Serializer):
    """A hazard clip to watch and click through."""

    type = serializers.ChoiceField(choices=[("hazard", "hazard")])
    title = serializers.CharField()
    clips = serializers.ListField(
        child=serializers.SlugField(),
        help_text="Clip slugs to look up in the response's `clips` map.",
    )


class HtmlBlockSerializer(serializers.Serializer):
    """Material to read. The HTML was cleaned against an allowlist before it was stored."""

    type = serializers.ChoiceField(choices=[("html", "html")])
    title = serializers.CharField(help_text="Shown as a heading when the lesson has more than one block.")
    html = serializers.CharField(help_text="Cleaned HTML. Any picture's src is a short-lived signed URL.")


class CheckBlockSerializer(serializers.Serializer):
    """A question to answer in the middle of a lesson."""

    type = serializers.ChoiceField(choices=[("check", "check")])
    title = serializers.CharField()
    questions = serializers.ListField(
        child=serializers.SlugField(),
        help_text="Question keys to look up in the response's `questions` map.",
    )


class SubtitleSerializer(serializers.Serializer):
    language = serializers.ChoiceField(
        choices=SubtitleLanguage.choices,
        help_text="A BCP 47 tag, for `srclang` — `en-GB`, `fr-FR`. More languages may be added later.",
    )
    label = serializers.CharField(help_text="The language's name in English, for the player's menu.")  # type: ignore[assignment]
    url = serializers.URLField(help_text="A short-lived signed URL for the WebVTT file.")


class SubtitleListField(serializers.ListField):
    def __init__(self, **kwargs):
        kwargs.setdefault("child", SubtitleSerializer())
        kwargs.setdefault(
            "help_text",
            "The video's subtitle tracks, one per language, empty when it has none. Only videos carry it.",
        )
        super().__init__(**kwargs)


class VideoProgressSerializer(serializers.Serializer):
    slug = serializers.SlugField(
        required=False, help_text="The video block's slug, required when progress is not linked to a block."
    )
    positionSeconds = serializers.FloatField(  # noqa: N815
        help_text="Seconds into the video to resume from. 0 when nothing has been saved."
    )
    durationSeconds = serializers.FloatField(  # noqa: N815
        allow_null=True, help_text="How long the video is, as the player last reported it. Null when it never did."
    )
    resumeAt = serializers.CharField(  # noqa: N815
        help_text="`positionSeconds` as a timecode for the player's label — `03:25`, or `1:02:03` past an hour."
    )
    hasProgress = serializers.BooleanField(  # noqa: N815
        help_text="False when this learner has no saved position for the video, so start from the beginning."
    )
    finished = serializers.BooleanField(
        help_text=(
            "True if the saved position is within 1.5 seconds of the end. Does not mark the lesson as complete."
        )
    )
    updatedAt = serializers.DateTimeField(  # noqa: N815
        allow_null=True, help_text="When the position was last saved. Null when there is none."
    )


class VideoProgressRequestSerializer(serializers.Serializer):
    ALIASES = {"position_seconds": "positionSeconds", "duration_seconds": "durationSeconds"}

    positionSeconds = serializers.DecimalField(  # noqa: N815
        max_digits=9,
        decimal_places=2,
        min_value=0,
        help_text="Seconds into the video. Clamped to the duration, and a negative value is refused.",
    )
    durationSeconds = serializers.DecimalField(  # noqa: N815
        max_digits=9,
        decimal_places=2,
        min_value=0,
        required=False,
        allow_null=True,
        help_text="How long the video is. Optional, and a duration saved earlier is kept when it is left out.",
    )

    def to_internal_value(self, data):
        if hasattr(data, "items"):
            renamed = data.dict() if hasattr(data, "dict") else dict(data)
            for alias, key in self.ALIASES.items():
                if alias in renamed and key not in renamed:
                    renamed[key] = renamed.pop(alias)
            data = renamed
        return super().to_internal_value(data)


class MediaBlockSerializer(serializers.Serializer):
    """A video to watch or a document to read.

    The URL is signed and expires, so it is fetched with the lesson and used straight away rather
    than stored or shared.
    """

    type = serializers.ChoiceField(choices=[("video", "video"), ("document", "document")])
    title = serializers.CharField()
    url = serializers.URLField(help_text="A short-lived signed URL. Do not cache it.")
    slug = serializers.SlugField(
        required=False,
        help_text="Only videos carry it: the slug to post to /api/learn/videos/<slug>/progress/.",
    )
    subtitles = SubtitleListField(required=False)
    progress = VideoProgressSerializer(
        required=False, help_text="The signed-in learner's own saved position. Only videos carry it."
    )


class SignsBlockSerializer(serializers.Serializer):
    type = serializers.ChoiceField(choices=[("signs", "signs")])
    caption = serializers.CharField(
        allow_blank=True, help_text="Shown under the strip. Blank when the text gave none."
    )
    signs = SignSerializer(many=True)


CONTENT_BLOCK = PolymorphicProxySerializer(
    component_name="ContentBlock",
    serializers={
        "html": HtmlBlockSerializer,
        "check": CheckBlockSerializer,
        "video": MediaBlockSerializer,
        "document": MediaBlockSerializer,
        "hazard": HazardBlockSerializer,
    },
    resource_type_field_name="type",
)


EBOOK_BLOCK = PolymorphicProxySerializer(
    component_name="EbookBlock",
    serializers={
        "html": HtmlBlockSerializer,
        "signs": SignsBlockSerializer,
        "video": MediaBlockSerializer,
        "document": MediaBlockSerializer,
    },
    resource_type_field_name="type",
)


@extend_schema_field(CONTENT_BLOCK)
class ContentBlockField(serializers.JSONField):
    """One block of a lesson, which the frontend renders according to its ``type``."""


@extend_schema_field(EBOOK_BLOCK)
class EbookBlockField(serializers.JSONField):
    """One block of an e-book section. The same kinds as a lesson, minus checks and clips."""


class BlockListField(serializers.ListField):
    def __init__(self, **kwargs):
        kwargs.setdefault("child", ContentBlockField())
        kwargs.setdefault("help_text", "The material, in reading order.")
        super().__init__(**kwargs)


class EbookBlockListField(serializers.ListField):
    def __init__(self, **kwargs):
        kwargs.setdefault("child", EbookBlockField())
        kwargs.setdefault(
            "help_text", "The material, in reading order, with the sign strips where the text asked for them."
        )
        super().__init__(**kwargs)


class LinkSerializer(serializers.Serializer):
    """Where "previous" and "next" go. Null at either end of a chapter."""

    slug = serializers.SlugField()
    title = serializers.CharField()


class HazardClipSerializer(serializers.Serializer):
    """A clip as a student is given it.

    No hazard timings: sending them would say exactly when to click, which is what is being
    tested. They come back with the score, once the attempt is over.
    """

    slug = serializers.CharField()
    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    url = serializers.URLField(help_text="A short-lived signed URL for the video. Do not cache it.")
    subtitles = SubtitleListField()
    thumbnailUrl = serializers.URLField(  # noqa: N815
        allow_null=True,
        help_text="A short-lived signed URL for the still, or null when the clip has none.",
    )
    hazards = serializers.IntegerField(help_text="How many hazards are in the clip, but not when.")
    topScore = serializers.IntegerField(help_text="The most this clip can score: five per hazard.")  # noqa: N815
    maxClicks = serializers.IntegerField(  # noqa: N815
        help_text="Clicking more often than this scores nothing, as in the real test."
    )
    attempts = serializers.IntegerField(
        required=False,
        help_text=(
            "How many times you have attempted this clip, 0 if never. Sent when practising the "
            "clips, left out inside a lesson or a paper being sat."
        ),
    )
    bestScore = serializers.IntegerField(  # noqa: N815
        required=False,
        allow_null=True,
        help_text="Your highest score on this clip, or null if you have never attempted it.",
    )


class HazardResultSerializer(serializers.Serializer):
    label = serializers.CharField(help_text="What the hazard was.")  # type: ignore[assignment]
    startsAt = serializers.FloatField()  # noqa: N815
    endsAt = serializers.FloatField()  # noqa: N815
    score = serializers.IntegerField(help_text="5 for spotting it earliest, down to 1, or 0 if missed.")
    spotted = serializers.BooleanField()


class ClipMapField(serializers.DictField):
    """The hazard clips a response's hazard blocks name, keyed by slug."""

    def __init__(self, **kwargs):
        kwargs.setdefault("child", HazardClipSerializer())
        kwargs.setdefault("help_text", "The clips this content shows, keyed by the slug its hazard blocks name.")
        super().__init__(**kwargs)


# --- Topics --------------------------------------------------------------------------------------


class LessonSummarySerializer(serializers.Serializer):
    """A lesson as it appears in the topic list: enough to show it, not to read it."""

    slug = serializers.SlugField()
    title = serializers.CharField()
    summary = serializers.CharField(allow_blank=True)
    minutes = serializers.IntegerField(help_text="Rough study time.")
    done = serializers.BooleanField(help_text="Always false until progress is recorded.")
    videoProgress = VideoProgressSerializer(  # noqa: N815
        allow_null=True,
        help_text=(
            "The video in this lesson the learner last watched, with their saved position, so the "
            "card can offer to resume it. Null when they have saved none."
        ),
    )


class TopicSerializer(serializers.Serializer):
    slug = serializers.SlugField()
    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    icon = serializers.CharField(allow_blank=True, help_text="An icon name the frontend knows.")
    mastery = serializers.IntegerField(help_text="Always 0 until progress is recorded.")
    questions = serializers.IntegerField(help_text="Always 0 until progress is recorded.")
    lessons = LessonSummarySerializer(many=True)


class TopicListResponseSerializer(serializers.Serializer):
    topics = TopicSerializer(many=True)


# --- A lesson ------------------------------------------------------------------------------------


class LessonSerializer(serializers.Serializer):
    slug = serializers.SlugField()
    title = serializers.CharField()
    summary = serializers.CharField(allow_blank=True)
    minutes = serializers.IntegerField()
    blocks = BlockListField()


class TopicRefSerializer(serializers.Serializer):
    slug = serializers.SlugField()
    title = serializers.CharField()
    icon = serializers.CharField(allow_blank=True)


class LessonDetailResponseSerializer(serializers.Serializer):
    lesson = LessonSerializer()
    topic = TopicRefSerializer()
    done = serializers.BooleanField(help_text="Always false until progress is recorded.")
    prev = LinkSerializer(allow_null=True)
    next = LinkSerializer(allow_null=True)
    signs = SignMapField(
        help_text=(
            "Empty today: lesson material names signs in its HTML rather than as data, so there is "
            "nothing to resolve. The key is sent so the frontend needs no special case for it."
        )
    )
    questions = QuestionMapField()
    clips = ClipMapField()


# --- The e-book ----------------------------------------------------------------------------------


class EbookSectionSummarySerializer(serializers.Serializer):
    slug = serializers.SlugField()
    title = serializers.CharField()
    summary = serializers.CharField(allow_blank=True)
    number = serializers.IntegerField(help_text="Its position in the book, counted from 1.")
    read = serializers.BooleanField(help_text="Whether this student has marked it read.")


class EbookSummarySerializer(serializers.Serializer):
    slug = serializers.SlugField(help_text="Ask for the book itself at /api/learn/ebook/<slug>/.")
    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    coverUrl = serializers.URLField(  # noqa: N815
        allow_null=True, help_text="A short-lived signed URL for the cover, or null without one."
    )
    updatedAt = serializers.DateTimeField(help_text="When staff last changed it.")  # noqa: N815


class EbookOnTheShelfSerializer(EbookSummarySerializer):
    """A book as the shelf lists it, with its contents but not its material."""

    hasDocument = serializers.BooleanField(  # noqa: N815
        help_text=(
            "Whether there is a file to download as well as, or instead of, sections. The shelf "
            "gets no link, so this is how a card knows whether to offer one."
        )
    )
    sections = EbookSectionSummarySerializer(
        many=True, help_text="Its sections in reading order. Empty for a book that is only a document."
    )
    current = serializers.SlugField(
        allow_null=True, help_text="Where this student left off in this book, by section slug."
    )


class EbookSerializer(EbookSummarySerializer):
    url = serializers.URLField(allow_null=True, help_text="A short-lived signed URL for the document.")
    contentType = serializers.CharField(  # noqa: N815
        allow_null=True, allow_blank=True, help_text="For example application/pdf."
    )
    sizeBytes = serializers.IntegerField(  # noqa: N815
        allow_null=True, help_text="So the reader can warn before a large download."
    )


class EbookListResponseSerializer(serializers.Serializer):
    ebooks = EbookOnTheShelfSerializer(
        many=True, help_text="The books managed in the Ebooks section of the back office."
    )


class EbookSectionSerializer(EbookSectionSummarySerializer):
    """A section as it is handed over to be read: its contents entry, plus the material itself."""

    blocks = EbookBlockListField()


class EbookDetailResponseSerializer(serializers.Serializer):
    """A whole book: its metadata, and every section the student may read, in order."""

    ebook = EbookSerializer()
    sections = EbookSectionSerializer(
        many=True, help_text="In reading order, each with the blocks a lesson would carry."
    )
    current = serializers.SlugField(allow_null=True, help_text="Where this student left off, by section slug.")


# --- Signs, practice and answering ---------------------------------------------------------------


class SignListResponseSerializer(serializers.Serializer):
    signs = SignSerializer(many=True)


class PracticeSetResponseSerializer(serializers.Serializer):
    questions = StudentQuestionSerializer(many=True)
    signs = SignMapField()
    saved = serializers.ListField(
        child=serializers.SlugField(),
        help_text="Keys of the questions this student has saved. Always empty until that is recorded.",
    )


class AnswerRequestSerializer(serializers.Serializer):
    """What to post to have an answer marked."""

    questionId = serializers.IntegerField(help_text="The question's `id` from the practice set.")  # noqa: N815
    selected = serializers.ListField(
        child=serializers.CharField(),
        help_text="The option ids the student chose, e.g. ['a']. Order does not matter.",
    )


class AnswerResponseSerializer(serializers.Serializer):
    """The marking, which only the server decides."""

    correct = serializers.BooleanField()
    correctIds = serializers.ListField(  # noqa: N815
        child=serializers.CharField(), help_text="The ids that were right, revealed now the student has answered."
    )
    explanation = serializers.CharField(allow_blank=True)
    lesson = LinkSerializer(allow_null=True, help_text="Where to read more about this, when there is somewhere.")


# --- Exams ---------------------------------------------------------------------------------------


class ExamSummarySerializer(serializers.Serializer):
    """An exam as it appears in the list: enough to choose one, not to sit it."""

    slug = serializers.SlugField()
    title = serializers.CharField()
    description = serializers.CharField(allow_blank=True)
    kind = serializers.ChoiceField(choices=ExamKind.choices)
    questionCount = serializers.IntegerField(help_text="How many questions will be asked.")  # noqa: N815
    passMark = serializers.IntegerField(  # noqa: N815
        allow_null=True,
        help_text="How many must be right to pass. Null on a paper of hazard clips alone, which asks none.",
    )
    hazardPassMark = serializers.IntegerField(  # noqa: N815
        allow_null=True,
        help_text=(
            "How many hazard marks pass the clips, scored apart from the questions and never "
            "added to them. Null when no figure has been configured for this exam."
        ),
    )
    timeLimitSeconds = serializers.IntegerField(  # noqa: N815
        allow_null=True, help_text="Null when the exam is untimed."
    )
    clipCount = serializers.IntegerField(  # noqa: N815
        help_text="How many hazard clips are sat after the questions. 0 for a question-only paper."
    )
    hazardMaxScore = serializers.IntegerField(  # noqa: N815
        help_text="The most the clips can score: five per hazard. 0 for a question-only paper."
    )
    attempts = serializers.IntegerField(help_text="How many times this student has sat it. 0 until they do.")
    bestPercent = serializers.IntegerField(  # noqa: N815
        help_text="Their best question score as a percentage, over their completed sittings. 0 until they sit it."
    )
    lastAttemptId = serializers.IntegerField(  # noqa: N815
        allow_null=True,
        help_text="Their latest sitting, to read back at /api/learn/exam-attempts/<id>/. Null until they sit it.",
    )


class ExamListResponseSerializer(serializers.Serializer):
    exams = ExamSummarySerializer(many=True)


class ExamDetailResponseSerializer(serializers.Serializer):
    """An exam to sit. The questions carry no answers; submit the sitting to have it marked."""

    exam = ExamSummarySerializer()
    questions = StudentQuestionSerializer(many=True, help_text="In the order staff arranged them.")
    signs = SignMapField()
    clips = HazardClipSerializer(
        many=True,
        help_text="The hazard clips to sit after the questions, in order. Empty for a question-only paper.",
    )


class TimeTakenField(serializers.IntegerField):
    def __init__(self, **kwargs):
        kwargs.setdefault("required", False)
        kwargs.setdefault("allow_null", True)
        super().__init__(**kwargs)

    def to_internal_value(self, data):
        try:
            return super().to_internal_value(data)
        except serializers.ValidationError:
            return None


class ExamSubmissionSerializer(serializers.Serializer):
    """A finished sitting, posted to be marked."""

    answers = serializers.DictField(
        child=serializers.ListField(child=serializers.CharField()),
        help_text='The option ids chosen for each question, keyed by the question\'s `id`, e.g. {"12": ["a"]}.',
    )
    hazardClicks = serializers.DictField(  # noqa: N815
        required=False,
        child=click_times(),
        help_text=(
            "For a mock test with clips: the seconds into each clip at which the student clicked, "
            'keyed by the clip\'s `slug`, e.g. {"country-lane": [4.2, 11.8]}. A clip left out '
            f"scores nothing, as an unanswered question does. At most {MAX_CLICKS_PER_SUBMISSION} "
            "clicks per clip."
        ),
    )
    timeTakenSeconds = TimeTakenField()  # noqa: N815


class ExamQuestionResultSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    key = serializers.SlugField()
    correct = serializers.BooleanField()
    selected = serializers.ListField(child=serializers.CharField(), help_text="Empty when it was left unanswered.")
    correctIds = serializers.ListField(child=serializers.CharField())  # noqa: N815
    explanation = serializers.CharField(allow_blank=True)


class ExamClipResultSerializer(serializers.Serializer):
    """How one hazard clip in the sitting was scored, revealed once it is over."""

    id = serializers.IntegerField()
    slug = serializers.SlugField()
    title = serializers.CharField()
    score = serializers.IntegerField()
    topScore = serializers.IntegerField(help_text="The most this clip was worth.")  # noqa: N815
    voided = serializers.BooleanField(help_text="True when the clicking was not accepted, so the clip scored nothing.")
    voidReason = serializers.ChoiceField(  # noqa: N815
        choices=HazardVoidReason.choices,
        allow_null=True,
        help_text="Why the clip was voided, or null when it counted.",
    )
    hazards = HazardResultSerializer(many=True, help_text="Every hazard, spotted or not, with its timing.")


class ExamStatisticsSerializer(serializers.Serializer):
    totalQuestions = serializers.IntegerField(help_text="Questions the exam asked.")  # noqa: N815
    correctQuestions = serializers.IntegerField()  # noqa: N815
    incorrectQuestions = serializers.IntegerField(help_text="Answered, but wrongly.")  # noqa: N815
    unansweredQuestions = serializers.IntegerField(help_text="Left blank. Counted as wrong.")  # noqa: N815
    hazardTotalScore = serializers.IntegerField(help_text="Scored on the clips. 0 without any.")  # noqa: N815
    hazardMaxScore = serializers.IntegerField(  # noqa: N815
        help_text="The most the clips could score: five per hazard. 0 without any."
    )


class ExamResultResponseSerializer(ExamStatisticsSerializer):
    score = serializers.IntegerField(help_text="Questions right. The same as `correctQuestions`.")
    total = serializers.IntegerField(help_text="Questions asked. The same as `totalQuestions`.")
    passMark = serializers.IntegerField(  # noqa: N815
        allow_null=True, help_text="Null on a paper of hazard clips alone, which asks no questions."
    )
    hazardPassMark = serializers.IntegerField(  # noqa: N815
        allow_null=True, help_text="The clips' own pass mark, or null when none is configured."
    )
    questionsPassed = serializers.BooleanField(  # noqa: N815
        help_text=(
            "Whether the question score reached `passMark`. Always true on a paper with no "
            "questions, which has no question half to fail on — the clips decide it."
        )
    )
    hazardPassed = serializers.BooleanField(  # noqa: N815
        allow_null=True,
        help_text=(
            "Whether the hazard score reached `hazardPassMark`. Null when there is nothing to "
            "judge: a question-only paper, or an exam with no hazard pass mark configured."
        ),
    )
    passed = serializers.BooleanField(
        help_text=(
            "The sitting as a whole: both parts must be passed. A paper whose clips cannot be "
            "judged is decided on the questions alone. The two scores are never added together."
        )
    )
    questions = ExamQuestionResultSerializer(many=True, help_text="Every question, answered or not.")
    clips = ExamClipResultSerializer(many=True, help_text="Every clip in the exam, clicked in or not.")
    attemptId = serializers.IntegerField(  # noqa: N815
        help_text="The sitting just recorded."
    )
    timeTakenSeconds = serializers.IntegerField(  # noqa: N815
        allow_null=True,
        help_text=(
            "How long the questions took, as it was recorded: seconds from starting the exam to "
            "ending it, the clips excluded. Null when the sitting was not timed."
        ),
    )


class ExamAttemptSummarySerializer(serializers.Serializer):
    id = serializers.IntegerField(help_text="Read its full results at /api/learn/exam-attempts/<id>/.")
    completedAt = serializers.DateTimeField(help_text="When it was marked, which is when it was finished.")  # noqa: N815
    score = serializers.IntegerField(help_text="Questions right.")
    total = serializers.IntegerField(help_text="Questions asked in that sitting.")
    passed = serializers.BooleanField()
    percent = serializers.IntegerField(help_text="The question score as a percentage of that sitting's total.")
    hazardScore = serializers.IntegerField(help_text="Scored on the clips. 0 for a question-only paper.")  # noqa: N815
    hazardMaxScore = serializers.IntegerField(  # noqa: N815
        help_text="The most that sitting's clips could score. 0 for a question-only paper."
    )
    timeTakenSeconds = serializers.IntegerField(  # noqa: N815
        allow_null=True,
        help_text="How long that sitting's questions took, in seconds. Null when it was not timed.",
    )


class ExamAttemptListResponseSerializer(serializers.Serializer):
    attempts = ExamAttemptSummarySerializer(many=True, help_text="Newest first.")
    bestPercent = serializers.IntegerField(help_text="The best percentage over these sittings. 0 when there are none.")  # noqa: N815
    lastAttemptId = serializers.IntegerField(allow_null=True, help_text="The newest sitting's id, or null.")  # noqa: N815


class ExamRefSerializer(serializers.Serializer):
    slug = serializers.SlugField()
    title = serializers.CharField()
    kind = serializers.ChoiceField(choices=ExamKind.choices)


class ExamIdentitySerializer(ExamRefSerializer):
    id = serializers.IntegerField(help_text="The exam this sitting was of.")


class ExamAttemptAcrossExamsSerializer(ExamAttemptSummarySerializer):
    exam = ExamIdentitySerializer(help_text="Which exam it was. `kind` tells a practice exam from a mock test.")
    questionsPassed = serializers.BooleanField(  # noqa: N815
        allow_null=True,
        help_text=(
            "Whether the question score reached the pass mark in force at the time. Null for a "
            "sitting recorded before the marked paper was kept."
        ),
    )
    hazardPassed = serializers.BooleanField(  # noqa: N815
        allow_null=True,
        help_text=(
            "Whether the hazard score reached the pass mark in force at the time. Null when there "
            "was nothing to judge, and for a sitting recorded before the marked paper was kept."
        ),
    )


class AllExamAttemptsResponseSerializer(serializers.Serializer):
    attempts = ExamAttemptAcrossExamsSerializer(
        many=True, help_text="Newest first, capped at the newest 50 across all exams."
    )
    count = serializers.IntegerField(help_text="How many sittings are listed, after the cap.")


class ExamAttemptResultResponseSerializer(ExamResultResponseSerializer):
    completedAt = serializers.DateTimeField()  # noqa: N815
    exam = ExamRefSerializer()


# --- Progress ------------------------------------------------------------------------------------


class ProgressSummaryResponseSerializer(serializers.Serializer):
    """The headline numbers for the Today screen, all worked out from the student's attempts."""

    streak = serializers.IntegerField(help_text="Days in a row studied. Today not being used yet does not break it.")
    studyDays = serializers.IntegerField(help_text="How many separate days they have studied.")  # noqa: N815
    lastStudiedOn = serializers.DateField(allow_null=True)  # noqa: N815
    questionsAnswered = serializers.IntegerField(help_text="Distinct questions attempted.")  # noqa: N815
    questionsLearnt = serializers.IntegerField(help_text="Distinct questions answered correctly.")  # noqa: N815
    mastery = serializers.IntegerField(help_text="Questions learnt as a percentage of the whole bank.")
    lessonsCompleted = serializers.IntegerField()  # noqa: N815
    sectionsRead = serializers.IntegerField(help_text="Sections of an e-book marked read.")  # noqa: N815
    mockAttempts = serializers.IntegerField(help_text="Mock tests sat. Practice exams are counted on their own.")  # noqa: N815
    mocksPassed = serializers.IntegerField()  # noqa: N815
    bestMockScore = serializers.IntegerField(allow_null=True)  # noqa: N815
    practiceAttempts = serializers.IntegerField(help_text="Practice exams sat.")  # noqa: N815
    practiceExamsPassed = serializers.IntegerField()  # noqa: N815
    bestPracticeScore = serializers.IntegerField(  # noqa: N815
        allow_null=True,
        help_text=(
            "The best practice exam sat, as a whole percentage of that sitting's questions — not "
            "a count of right answers, because papers ask different numbers of questions. Null "
            "until one has been sat. Hazard marks are never part of it."
        ),
    )
    bestPracticeCorrect = serializers.IntegerField(  # noqa: N815
        allow_null=True, help_text="Questions right in that same sitting. Null until one has been sat."
    )
    bestPracticeTotal = serializers.IntegerField(  # noqa: N815
        allow_null=True, help_text="Questions that same sitting asked. Null until one has been sat."
    )
    hazardAttempts = serializers.IntegerField()  # noqa: N815
    bestHazardScore = serializers.IntegerField(allow_null=True)  # noqa: N815


class DoneResponseSerializer(serializers.Serializer):
    """The reply to marking something done. Marking it twice says the same thing."""

    done = serializers.BooleanField()


class ReadResponseSerializer(serializers.Serializer):
    """The reply to marking an e-book section read. Marking it twice says the same thing."""

    read = serializers.BooleanField()


class SavedResponseSerializer(serializers.Serializer):
    saved = serializers.BooleanField()


# --- Hazard perception ---------------------------------------------------------------------------


class HazardClipListResponseSerializer(serializers.Serializer):
    clips = HazardClipSerializer(many=True)


class HazardAttemptRequestSerializer(serializers.Serializer):
    """A finished attempt, posted to be scored."""

    clicks = click_times(
        allow_empty=True,
        help_text=(
            "Seconds into the clip at which the student clicked, e.g. [4.2, 11.8]. "
            f"At most {MAX_CLICKS_PER_SUBMISSION} of them."
        ),
    )


class HazardAttemptResponseSerializer(serializers.Serializer):
    score = serializers.IntegerField()
    topScore = serializers.IntegerField()  # noqa: N815
    voided = serializers.BooleanField(help_text="True when the clicking was not accepted, so the clip scored nothing.")
    voidReason = serializers.ChoiceField(  # noqa: N815
        choices=HazardVoidReason.choices,
        allow_null=True,
        help_text="Why the attempt was voided, or null when it counted.",
    )
    hazards = HazardResultSerializer(many=True, help_text="Every hazard, spotted or not, with its timing.")
