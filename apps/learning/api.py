"""The endpoints students use to read the learning material.

What a student is served is decided twice over, and never by the browser:

* only material that is **live** — published, and past its scheduled time;
* only material their **package** includes, checked against the item, its subchapter and its
  chapter, so a package that excludes a chapter excludes everything inside it.

The frontend keeps its own words. A chapter is a "topic" and a subchapter is a "lesson" in these
responses, so renaming things inside the product did not reach the API. Keys are camelCase and a
lapsed plan answers 402, matching the contract the frontend already implements.
"""

from typing import cast

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.entitlements import services as entitlements
from apps.learning import services
from apps.learning.models import (
    Chapter,
    ExamKind,
    HazardClip,
    MockAttempt,
    PracticeExam,
    Question,
    SavedQuestion,
    Sign,
    Subchapter,
)
from apps.learning.serializers import (
    EBOOK_DETAIL,
    AnswerRequestSerializer,
    AnswerResponseSerializer,
    DoneResponseSerializer,
    EbookListResponseSerializer,
    ExamAttemptListResponseSerializer,
    ExamAttemptResultResponseSerializer,
    ExamDetailResponseSerializer,
    ExamListResponseSerializer,
    ExamResultResponseSerializer,
    ExamSubmissionSerializer,
    HazardAttemptRequestSerializer,
    HazardAttemptResponseSerializer,
    HazardClipListResponseSerializer,
    HazardClipSerializer,
    LessonDetailResponseSerializer,
    PracticeSetResponseSerializer,
    ProgressSummaryResponseSerializer,
    SavedResponseSerializer,
    SignListResponseSerializer,
    TopicListResponseSerializer,
)

PLAN_NEEDED = "An active plan is needed for this"
HIGHWAY_CODE_SLUG = "highway-code"
# The modes the practice endpoint accepts. The ones that depend on past attempts are accepted
# but practise everything, because progress is not recorded yet.
MODES = ["random", "topic", "mistakes", "weak", "saved", "review"]


def plan_needed() -> Response:
    """402 rather than 403: the student is signed in, they simply have nothing to read with."""
    return Response({"detail": PLAN_NEEDED}, status=status.HTTP_402_PAYMENT_REQUIRED)


def sign_map(codes) -> dict:
    """The signs a payload mentions, by code, so the frontend can draw them without asking again."""
    return {
        sign.code: {
            "code": sign.code,
            "name": sign.name,
            "category": sign.category,
            "meaning": sign.meaning,
            "spec": sign.spec,
        }
        for sign in Sign.objects.filter(code__in=codes)
    }


class TopicListView(APIView):
    """The catalogue: chapters and the subchapters inside them, titles only.

    Readable without a plan, so somebody deciding what to buy can see what is on offer. The
    material itself is not here.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="listTopics",
        summary="List the topics and their lessons",
        responses={200: TopicListResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        student = cast(User, request.user)

        # Asked once. Neither answer can change while this request is being served, and asking
        # per chapter is what made the whole course take seconds to list.
        has_access = entitlements.has_access(student)
        package_id = entitlements.package_of(student) if has_access else None

        chapters = services.live(Chapter.objects.all()).order_by("order", "title")
        if has_access:
            chapters = entitlements.visible_chapters(student, chapters)
        chapters = list(chapters)

        done = services.completed_lesson_ids(student)
        mastery = services.mastery_by_chapter(student, chapters)
        lessons = self._lessons_by_chapter(student, chapters, has_access, package_id)

        return Response(
            {
                "topics": [
                    {
                        "slug": chapter.slug,
                        "title": chapter.title,
                        "description": chapter.description,
                        "icon": chapter.icon,
                        **mastery[chapter.pk],
                        "lessons": [
                            {
                                "slug": subchapter.slug,
                                "title": subchapter.title,
                                "summary": subchapter.summary,
                                "minutes": subchapter.minutes,
                                "done": subchapter.pk in done,
                            }
                            for subchapter in lessons.get(chapter.pk, [])
                        ],
                    }
                    for chapter in chapters
                ]
            }
        )

    def _lessons_by_chapter(self, student, chapters, has_access, package_id) -> dict[int, list]:
        """Every listed chapter's live subchapters, in one query, grouped by chapter.

        Fetching them a chapter at a time is correct but costs a round trip each, and the
        database is not local to the web server.
        """
        if not chapters:
            return {}

        subchapters = services.live(Subchapter.objects.filter(chapter__in=chapters)).order_by(
            "chapter_id", "order", "title"
        )
        if has_access:
            subchapters = entitlements.visible_subchapters(student, subchapters, package_id=package_id)

        grouped: dict[int, list] = {}
        for subchapter in subchapters:
            grouped.setdefault(subchapter.chapter_id, []).append(subchapter)
        return grouped


class LessonDetailView(APIView):
    """One subchapter with its material, which is what a student actually reads."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="getLesson",
        summary="Read a lesson",
        responses={200: LessonDetailResponseSerializer},
    )
    def get(self, request: Request, slug: str) -> Response:
        student = cast(User, request.user)
        subchapter = services.live(Subchapter.objects.select_related("chapter")).filter(slug=slug).first()
        if subchapter is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if not entitlements.can_view(student, subchapter):
            # The same answer whether the plan lapsed or the package never included this, so the
            # endpoint cannot be used to map out what other packages contain.
            return plan_needed()

        content = entitlements.visible_content(student, services.live_content_of(subchapter)).order_by(
            "order", "title"
        )

        neighbours = list(services.live_subchapters_of(subchapter.chapter).order_by("order", "title"))
        position = next((i for i, item in enumerate(neighbours) if item.pk == subchapter.pk), None)

        return Response(
            {
                "lesson": {
                    "slug": subchapter.slug,
                    "title": subchapter.title,
                    "summary": subchapter.summary,
                    "minutes": subchapter.minutes,
                    "blocks": [services.block_for(item) for item in content],
                },
                "topic": {
                    "slug": subchapter.chapter.slug,
                    "title": subchapter.chapter.title,
                    "icon": subchapter.chapter.icon,
                },
                "done": subchapter.pk in services.completed_lesson_ids(student),
                "prev": _link(neighbours, position, -1),
                "next": _link(neighbours, position, 1),
                "signs": {},
                "questions": _questions_in(content),
                "clips": _clips_in(content),
            }
        )


def _clips_in(content) -> dict:
    """The hazard clips a lesson's hazard blocks name, by slug.

    Sent with the lesson so the frontend has the video URL in one response, and without the
    hazard timings, which only come back with a score.
    """
    clips = [item.hazard_clip for item in content if item.hazard_clip_id and item.hazard_clip.is_published]
    return {clip.slug: services.student_view_of_clip(clip) for clip in clips}


def _questions_in(content) -> dict:
    """The questions a lesson's check blocks name, by key.

    Sent alongside the blocks so the frontend has everything in one response, and without the
    answers: those only arrive once the student has committed to one.
    """
    questions = [item.question for item in content if item.question_id and item.question.is_published]
    return {question.key: services.student_view_of_question(question) for question in questions}


def _link(neighbours: list, position: int | None, step: int) -> dict | None:
    """The previous or next lesson in the same chapter, or None at either end."""
    if position is None:
        return None
    wanted = position + step
    if wanted < 0 or wanted >= len(neighbours):
        return None
    return {"slug": neighbours[wanted].slug, "title": neighbours[wanted].title}


class EbookView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="listChapters",
        summary="List the e-books and the e-book chapters",
        responses={200: EbookListResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        student = cast(User, request.user)
        shelf = [services.ebook_on_the_shelf(ebook) for ebook in services.live_ebooks()]

        book = services.live(Chapter.objects.all()).filter(slug=HIGHWAY_CODE_SLUG).first()
        if book is None:
            return Response({"ebooks": shelf, "chapters": [], "current": None})

        sections = entitlements.visible_subchapters(student, services.live_subchapters_of(book)).order_by(
            "order", "title"
        )

        read = services.completed_lesson_ids(student)
        chapters = [
            {
                "slug": section.slug,
                "title": section.title,
                "summary": section.summary,
                "number": number,
                "read": section.pk in read,
            }
            for number, section in enumerate(sections, start=1)
        ]

        return Response(
            {
                "ebooks": shelf,
                "chapters": chapters,
                # The first section not yet read, which is where "continue reading" goes. The
                # last section once the book is finished, so the button always has a target.
                "current": next(
                    (item["slug"] for item in chapters if not item["read"]),
                    chapters[-1]["slug"] if chapters else None,
                ),
            }
        )


class EbookChapterView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="getChapter",
        summary="Read an e-book, or an e-book chapter",
        responses={200: EBOOK_DETAIL},
    )
    def get(self, request: Request, slug: str) -> Response:
        student = cast(User, request.user)

        ebook = services.live_ebooks().filter(slug=slug).first()
        if ebook is not None:
            # The file's signed URL is the thing being asked for, so this is where a plan is
            # needed — browsing the shelf is not.
            if not entitlements.has_access(student):
                return plan_needed()
            return Response({"ebook": services.student_view_of_ebook(ebook)})

        section = (
            services.live(Subchapter.objects.select_related("chapter"))
            .filter(slug=slug, chapter__slug=HIGHWAY_CODE_SLUG)
            .first()
        )
        if section is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if not entitlements.can_view(student, section):
            return plan_needed()

        content = entitlements.visible_content(student, services.live_content_of(section)).order_by("order", "title")
        siblings = list(services.live_subchapters_of(section.chapter).order_by("order", "title"))
        position = next((i for i, item in enumerate(siblings) if item.pk == section.pk), None)

        return Response(
            {
                "chapter": {
                    "slug": section.slug,
                    "title": section.title,
                    "summary": section.summary,
                    "number": (position or 0) + 1,
                    "blocks": [services.block_for(item) for item in content],
                },
                "toc": [
                    {"slug": item.slug, "title": item.title, "number": number}
                    for number, item in enumerate(siblings, start=1)
                ],
                "prev": _link(siblings, position, -1),
                "next": _link(siblings, position, 1),
                "signs": {},
                "questions": {},
                "clips": {},
            }
        )


class SignListView(APIView):
    """The road sign library, drawn by the frontend from each sign's specification."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="listSigns",
        summary="List the road signs",
        responses={200: SignListResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        student = cast(User, request.user)
        if not entitlements.has_access(student):
            return plan_needed()

        signs = Sign.objects.all().order_by("category", "order", "name")
        return Response({"signs": list(sign_map(signs.values_list("code", flat=True)).values())})


class PracticeSetView(APIView):
    """A set of questions to practise with, without their answers."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="getPracticeSet",
        summary="Get a set of practice questions",
        parameters=[
            OpenApiParameter("mode", str, OpenApiParameter.QUERY, enum=MODES, default="random"),
            OpenApiParameter(
                "topic", str, OpenApiParameter.QUERY, description="A chapter slug. Required when mode=topic."
            ),
            OpenApiParameter("count", int, OpenApiParameter.QUERY, default=10, description="Clamped to 1-50."),
        ],
        responses={200: PracticeSetResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        student = cast(User, request.user)
        if not entitlements.has_access(student):
            return plan_needed()

        mode = request.query_params.get("mode", "random")
        count = _count_from(request.query_params.get("count"))

        questions = services.live(Question.objects.select_related("chapter").prefetch_related("options"))
        questions = entitlements.visible_questions(student, questions)

        if mode == "topic":
            topic = request.query_params.get("topic")
            if not Chapter.objects.filter(slug=topic).exists():
                return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
            questions = questions.filter(chapter__slug=topic)

        if mode == "mistakes":
            questions = questions.filter(pk__in=services.question_ids_answered_wrong(student))
        elif mode == "saved":
            questions = questions.filter(saved_by__student=student)
        elif mode == "weak":
            questions = questions.exclude(pk__in=services.question_ids_already_learnt(student))
        elif mode == "review":
            # Everything already attempted, right or wrong. A subquery rather than a join: a
            # join returns one row per attempt, and the random ordering defeats .distinct().
            questions = questions.filter(pk__in=services.question_ids_attempted(student))

        chosen = list(questions.order_by("?")[:count])

        return Response(
            {
                "questions": [services.student_view_of_question(question) for question in chosen],
                "signs": sign_map(_sign_codes_in(chosen)),
                "saved": services.saved_question_keys(student),
            }
        )


def _count_from(raw: str | None) -> int:
    """How many questions to send back, kept inside sensible bounds whatever was asked for."""
    try:
        count = int(raw) if raw else 10
    except ValueError:
        count = 10
    return max(1, min(count, 50))


def _sign_codes_in(questions: list) -> set[str]:
    """Every sign a set of questions mentions, so the frontend can draw them all."""
    codes = set()
    for question in questions:
        if question.media_sign_id:
            codes.add(question.media_sign.code)
        codes.update(option.sign.code for option in question.options.all() if option.sign_id)
    return codes


class AnswerView(APIView):
    """Mark an answer and reveal what the right one was."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="checkAnswer",
        summary="Check an answer",
        request=AnswerRequestSerializer,
        responses={200: AnswerResponseSerializer},
    )
    def post(self, request: Request) -> Response:
        student = cast(User, request.user)
        if not entitlements.has_access(student):
            return plan_needed()

        question = (
            services.live(Question.objects.prefetch_related("options"))
            .filter(pk=request.data.get("questionId"))
            .first()
        )
        if question is None or not entitlements.can_view(student, question):
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        selected = request.data.get("selected") or []
        if not isinstance(selected, list):
            return Response({"detail": "selected must be a list of option ids."}, status=400)

        # Marked on the server: the answer was never in the page for the student to read.
        marked = services.mark_answer(question, [str(option) for option in selected])
        services.record_answer(student, question, marked["correct"])
        return Response(marked)


def _exam_summary(
    exam: PracticeExam, question_count: int, hazards: dict | None = None, history: dict | None = None
) -> dict:
    hazards = hazards or {}
    # An exam this student has never sat, which is every exam until they sit one.
    history = history or {"attempts": 0, "bestPercent": 0, "lastAttemptId": None}
    return {
        "slug": exam.slug,
        "title": exam.title,
        "description": exam.description,
        "kind": exam.kind,
        "questionCount": question_count,
        "passMark": exam.pass_mark,
        "hazardPassMark": exam.hazard_pass_mark,
        "timeLimitSeconds": exam.time_limit_seconds,
        "clipCount": hazards.get("clipCount", 0),
        "hazardMaxScore": hazards.get("hazardMaxScore", 0),
        **history,
    }


def _wholly_included(student, exam: PracticeExam) -> tuple[bool, list]:
    """Whether the student's package covers every question in the exam, and those questions.

    An exam is all-or-nothing. Serving a shortened version would make its pass mark unreachable,
    and leaving the shortened version out of the response would leak which chapters a dearer
    package contains, so an exam a student is not entitled to in full is simply not offered.
    """
    asked = list(services.questions_in_exam(exam))
    included = entitlements.visible_questions(student, services.questions_in_exam(exam)).count()
    return len(asked) == included, asked


class ExamListView(APIView):
    """The practice exams and mock tests on offer."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="listExams",
        summary="List the practice exams and mock tests",
        parameters=[
            OpenApiParameter(
                "kind", str, OpenApiParameter.QUERY, enum=[kind for kind, _ in ExamKind.choices], required=False
            )
        ],
        responses={200: ExamListResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        student = cast(User, request.user)
        if not entitlements.has_access(student):
            return plan_needed()

        exams = services.live(PracticeExam.objects.all()).order_by("order", "title")
        kind = request.query_params.get("kind")
        if kind:
            exams = exams.filter(kind=kind)

        exams = list(exams)
        hazards = services.hazard_totals_for(exams)
        history = services.history_for(student, exams)

        listed = []
        for exam in exams:
            included, asked = _wholly_included(student, exam)
            if included:
                listed.append(_exam_summary(exam, len(asked), hazards.get(exam.pk), history.get(exam.pk)))

        return Response({"exams": listed})


class ExamDetailView(APIView):
    """One exam to sit: its questions in order, without their answers."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="getExam",
        summary="Start an exam",
        responses={200: ExamDetailResponseSerializer},
    )
    def get(self, request: Request, slug: str) -> Response:
        student = cast(User, request.user)
        if not entitlements.has_access(student):
            return plan_needed()

        exam = services.live(PracticeExam.objects.all()).filter(slug=slug).first()
        if exam is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        included, asked = _wholly_included(student, exam)
        if not included:
            return plan_needed()

        clips = list(services.clips_in_exam(exam))

        return Response(
            {
                "exam": _exam_summary(
                    exam,
                    len(asked),
                    {"clipCount": len(clips), "hazardMaxScore": sum(clip.top_score for clip in clips)},
                    services.history_for(student, [exam]).get(exam.pk),
                ),
                "questions": [services.student_view_of_question(question) for question in asked],
                "signs": sign_map(_sign_codes_in(asked)),
                "clips": [services.student_view_of_clip(clip) for clip in clips],
            }
        )


class ExamSubmitView(APIView):
    """Mark a finished sitting."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="submitExam",
        summary="Submit an exam to be marked",
        request=ExamSubmissionSerializer,
        responses={200: ExamResultResponseSerializer},
    )
    def post(self, request: Request, slug: str) -> Response:
        student = cast(User, request.user)
        if not entitlements.has_access(student):
            return plan_needed()

        exam = services.live(PracticeExam.objects.all()).filter(slug=slug).first()
        if exam is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        included, _ = _wholly_included(student, exam)
        if not included:
            return plan_needed()

        submission = ExamSubmissionSerializer(data=request.data)
        submission.is_valid(raise_exception=True)

        # Marked on the server against every question the exam asks and every clip it shows, so a
        # sitting cannot be passed by sending back fewer answers than there were questions, nor a
        # hazard score claimed for a clip that was never clicked in.
        result = services.mark_exam(
            exam,
            submission.validated_data["answers"],
            submission.validated_data.get("hazardClicks"),
        )
        attempt = services.record_sitting(student, exam, result)

        return Response({**result, "attemptId": attempt.pk})


class ExamAttemptListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="listExamAttempts",
        summary="Your attempts at an exam",
        responses={200: ExamAttemptListResponseSerializer},
    )
    def get(self, request: Request, slug: str) -> Response:
        student = cast(User, request.user)
        if not entitlements.has_access(student):
            return plan_needed()

        exam = services.live(PracticeExam.objects.all()).filter(slug=slug).first()
        if exam is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        attempts = [services.attempt_in_the_history(attempt) for attempt in services.attempts_of(student, exam)]

        return Response(
            {
                "attempts": attempts,
                "bestPercent": max((attempt["percent"] for attempt in attempts), default=0),
                "lastAttemptId": attempts[0]["id"] if attempts else None,
            }
        )


class ExamAttemptView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="getExamAttempt",
        summary="Read the results of a past attempt",
        responses={200: ExamAttemptResultResponseSerializer},
    )
    def get(self, request: Request, attempt_id: int) -> Response:
        student = cast(User, request.user)

        attempt = MockAttempt.objects.select_related("exam").filter(pk=attempt_id, student=student).first()
        if attempt is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(services.saved_result_of(attempt))


class LessonCompleteView(APIView):
    """Mark a lesson as finished.

    An explicit call rather than guessed from the lesson being fetched, because opening a page is
    not reading it — and the student pressing the button is the only thing that actually says so.
    """

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="completeLesson",
        summary="Mark a lesson as finished",
        request=None,
        responses={200: DoneResponseSerializer},
    )
    def post(self, request: Request, slug: str) -> Response:
        student = cast(User, request.user)
        subchapter = services.live(Subchapter.objects.select_related("chapter")).filter(slug=slug).first()
        if subchapter is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        if not entitlements.can_view(student, subchapter):
            return plan_needed()

        services.mark_lesson_done(student, subchapter)
        return Response({"done": True})


class SavedQuestionView(APIView):
    """Put a question aside to come back to, or take it off the list."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="saveQuestion", summary="Save a question", request=None, responses={200: SavedResponseSerializer}
    )
    def post(self, request: Request, key: str) -> Response:
        student, question = self._find(request, key)
        if question is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        SavedQuestion.objects.get_or_create(student=student, question=question)
        return Response({"saved": True})

    @extend_schema(
        operation_id="unsaveQuestion", summary="Unsave a question", responses={200: SavedResponseSerializer}
    )
    def delete(self, request: Request, key: str) -> Response:
        student, question = self._find(request, key)
        if question is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        SavedQuestion.objects.filter(student=student, question=question).delete()
        return Response({"saved": False})

    def _find(self, request: Request, key: str) -> tuple:
        """The question, if this student is allowed it. Addressed by key, as content refers to it."""
        student = cast(User, request.user)
        if not entitlements.has_access(student):
            return student, None

        question = services.live(Question.objects.all()).filter(key=key).first()
        if question is None or not entitlements.can_view(student, question):
            return student, None
        return student, question


class ProgressView(APIView):
    """How the student is getting on, for the Today screen."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="getProgress",
        summary="Your progress so far",
        responses={200: ProgressSummaryResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        return Response(services.progress_summary(cast(User, request.user)))


class HazardClipListView(APIView):
    """The hazard clips a student can practise with."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="listHazardClips",
        summary="List the hazard perception clips",
        responses={200: HazardClipListResponseSerializer},
    )
    def get(self, request: Request) -> Response:
        student = cast(User, request.user)
        if not entitlements.has_access(student):
            return plan_needed()

        clips = services.live(HazardClip.objects.select_related("media")).order_by("order", "title")
        return Response({"clips": [services.student_view_of_clip(clip) for clip in clips]})


class HazardClipView(APIView):
    """One clip to watch."""

    permission_classes = [IsAuthenticated]

    @extend_schema(operation_id="getHazardClip", summary="Watch a hazard clip", responses={200: HazardClipSerializer})
    def get(self, request: Request, slug: str) -> Response:
        student = cast(User, request.user)
        clip = self._find(student, slug)
        if clip is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(services.student_view_of_clip(clip))

    def _find(self, student, slug: str):
        if not entitlements.has_access(student):
            return None
        return services.live(HazardClip.objects.select_related("media")).filter(slug=slug).first()


class HazardAttemptView(APIView):
    """Score an attempt at a clip."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="submitHazardAttempt",
        summary="Submit an attempt at a hazard clip",
        request=HazardAttemptRequestSerializer,
        responses={200: HazardAttemptResponseSerializer},
    )
    def post(self, request: Request, slug: str) -> Response:
        student = cast(User, request.user)
        if not entitlements.has_access(student):
            return plan_needed()

        clip = services.live(HazardClip.objects.prefetch_related("windows")).filter(slug=slug).first()
        if clip is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        attempt = HazardAttemptRequestSerializer(data=request.data)
        attempt.is_valid(raise_exception=True)

        # Scored on the server: the client was never told when the hazards were.
        result = services.score_clip(clip, attempt.validated_data["clicks"])
        services.record_hazard_attempt(student, clip, result)
        return Response(result)
