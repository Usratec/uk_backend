import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from apps.entitlements import services as entitlements
from apps.learning import services
from apps.learning.models import (
    Chapter,
    MediaAsset,
    Question,
    QuestionAttempt,
    QuestionOption,
    Sign,
    Subchapter,
)
from apps.notifications import services as notifications

logger = logging.getLogger(__name__)

MAX_BATCH_ITEMS = 500
MAX_PAGE_SIZE = 500
MAX_OPTIONS_PER_ANSWER = 20
FUTURE_GRACE = timedelta(minutes=2)
MAX_BACKDATE = timedelta(days=30)


class Status:
    SYNCED = "synced"
    ALREADY_SYNCED = "already_synced"
    REJECTED = "rejected"


class Error:
    MALFORMED = "malformed"
    ANSWERED_AT_IN_FUTURE = "answered_at_in_future"
    ANSWERED_AT_TOO_OLD = "answered_at_too_old"
    ANSWERED_AT_NOT_AWARE = "answered_at_not_timezone_aware"
    NO_ACCESS_ON_THAT_DATE = "no_access_on_that_date"
    QUESTION_NOT_FOUND = "question_not_found"
    INVALID_SELECTION = "invalid_selection"
    CLIENT_ID_CONFLICT = "client_id_conflict"


MESSAGES = {
    Error.MALFORMED: "This answer is not shaped the way the endpoint expects.",
    Error.ANSWERED_AT_IN_FUTURE: "The answer timestamp cannot be in the future.",
    Error.ANSWERED_AT_TOO_OLD: f"The answer timestamp cannot be more than {MAX_BACKDATE.days} days old.",
    Error.ANSWERED_AT_NOT_AWARE: "The answer timestamp needs a timezone offset, e.g. 2026-11-02T14:30:00Z.",
    Error.NO_ACCESS_ON_THAT_DATE: "There was no active plan covering this question on the date it was answered.",
    Error.QUESTION_NOT_FOUND: "That question is not one this student can practise.",
    Error.INVALID_SELECTION: "Those answer options do not belong to this question.",
    Error.CLIENT_ID_CONFLICT: "That clientId has already been used for a different answer.",
}

ERROR_CODES = tuple(MESSAGES)


def practice_questions(student, at=None):
    questions = services.live(
        Question.objects.select_related("chapter", "media_sign", "media_image", "learn_more").prefetch_related(
            "options__sign", "options__image"
        ),
        at=at,
    )
    return entitlements.visible_questions(student, questions, at=at).order_by("pk")


def question_with_its_answer(question: Question) -> dict:
    return {
        **services.student_view_of_question(question),
        "correctIds": question.correct_option_ids,
        "explanation": question.explanation,
        "lesson": (
            {"slug": question.learn_more.slug, "title": question.learn_more.title} if question.learn_more else None
        ),
    }


def sign_codes_in(questions) -> set[str]:
    codes = set()
    for question in questions:
        if question.media_sign_id:
            codes.add(question.media_sign.code)
        codes.update(option.sign.code for option in question.options.all() if option.sign_id)
    return codes


def pack_stamp(student, questions) -> dict:
    ids = questions.values("pk")

    parts = [
        _counted(questions.aggregate(n=Count("pk", distinct=True), changed=Max("updated_at"))),
        _counted(QuestionOption.objects.filter(question__in=ids).aggregate(n=Count("pk"), changed=Max("updated_at"))),
        _counted(
            Chapter.objects.filter(questions__in=ids).aggregate(
                n=Count("pk", distinct=True), changed=Max("updated_at")
            )
        ),
        _counted(
            Subchapter.objects.filter(questions__in=ids).aggregate(
                n=Count("pk", distinct=True), changed=Max("updated_at")
            )
        ),
        _counted(
            Sign.objects.filter(Q(questions__in=ids) | Q(answer_options__question__in=ids)).aggregate(
                n=Count("pk", distinct=True), changed=Max("updated_at")
            )
        ),
        _counted(
            MediaAsset.objects.filter(Q(questions__in=ids) | Q(answer_options__question__in=ids)).aggregate(
                n=Count("pk", distinct=True), changed=Max("updated_at")
            )
        ),
    ]

    newest = max((changed for _, changed in parts if changed is not None), default=None)
    counted = "|".join(count for count, _ in parts)
    digest = hashlib.sha256(f"{counted}|package:{entitlements.package_of(student)}".encode()).hexdigest()

    return {"version": digest[:16], "updatedAt": newest}


def _counted(aggregate: dict) -> tuple[str, datetime | None]:
    changed = aggregate["changed"]
    return f"{aggregate['n']}@{changed.isoformat() if changed else '-'}", changed


def pack_page(questions, *, after: int | None = None, limit: int | None = None) -> tuple[list[Question], int | None]:
    page = questions.filter(pk__gt=after) if after is not None else questions
    if limit is None:
        return list(page), None

    found = list(page[: limit + 1])
    if len(found) > limit:
        return found[:limit], found[limit - 1].pk
    return found, None


@dataclass(frozen=True)
class Answer:
    client_id: str
    question_id: int
    selected: list[str]
    answered_at: datetime


def digest_of(answer: Answer) -> str:
    chosen = ",".join(sorted(answer.selected))
    raw = f"{answer.question_id}|{chosen}|{answer.answered_at.astimezone(UTC).isoformat()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def rejected(client_id: str, code: str, detail=None) -> dict:
    error: dict = {"code": code, "message": MESSAGES[code]}
    if detail:
        error["detail"] = detail
    return {"clientId": client_id, "status": Status.REJECTED, "error": error}


def sync(student, answers: list[Answer | dict], at=None) -> list[dict]:
    at = at or timezone.now()
    wanted = [answer for answer in answers if isinstance(answer, Answer)]

    already = {
        attempt.client_id: attempt
        for attempt in QuestionAttempt.objects.filter(
            student=student, client_id__in=[answer.client_id for answer in wanted]
        ).select_related("question")
    }
    questions = _practisable(student, {answer.question_id for answer in wanted}, at)
    periods = entitlements.access_periods(student)

    results = []
    recorded = False
    for item in answers:
        if isinstance(item, dict):
            results.append(item)
            continue

        result = _sync_one(student, item, at=at, already=already, questions=questions, periods=periods)
        recorded = recorded or result["status"] == Status.SYNCED
        results.append(result)

    if recorded:
        notifications.queue_streak_check(student)

    return results


def _practisable(student, question_ids: set[int], at) -> dict[int, Question]:
    if not question_ids:
        return {}

    questions = practice_questions(student, at=at).filter(pk__in=question_ids)
    return {question.pk: question for question in questions.prefetch_related("chapter__only_for_packages")}


def _sync_one(student, answer: Answer, *, at, already, questions, periods) -> dict:
    digest = digest_of(answer)

    seen = already.get(answer.client_id)
    if seen is not None:
        return _already_synced(seen, answer, digest)

    if answer.answered_at > at + FUTURE_GRACE:
        return rejected(answer.client_id, Error.ANSWERED_AT_IN_FUTURE)

    if answer.answered_at < at - MAX_BACKDATE:
        return rejected(answer.client_id, Error.ANSWERED_AT_TOO_OLD)

    question = questions.get(answer.question_id)
    if question is None:
        return rejected(answer.client_id, Error.QUESTION_NOT_FOUND)

    if not entitlements.could_practise(student, question, answer.answered_at, periods=periods):
        return rejected(answer.client_id, Error.NO_ACCESS_ON_THAT_DATE)

    known = {option.option_id for option in question.options.all()}
    chosen = answer.selected
    if len(set(chosen)) != len(chosen) or not set(chosen) <= known:
        return rejected(answer.client_id, Error.INVALID_SELECTION, detail=sorted(known))

    marked = services.mark_answer(question, chosen)

    try:
        with transaction.atomic():
            attempt = services.record_answer(
                student,
                question,
                marked["correct"],
                answered_at=answer.answered_at,
                client_id=answer.client_id,
                client_payload=digest,
                notify=False,
            )
    except IntegrityError:
        seen = (
            QuestionAttempt.objects.filter(student=student, client_id=answer.client_id)
            .select_related("question")
            .first()
        )
        if seen is None:
            logger.exception("Could not record offline answer %s for student %s", answer.client_id, student.pk)
            raise
        already[answer.client_id] = seen
        return _already_synced(seen, answer, digest)

    already[answer.client_id] = attempt
    return {
        "clientId": answer.client_id,
        "status": Status.SYNCED,
        "correct": marked["correct"],
        "correctIds": marked["correctIds"],
        "answeredAt": answer.answered_at,
    }


def _already_synced(seen: QuestionAttempt, answer: Answer, digest: str) -> dict:
    if seen.client_payload and seen.client_payload != digest:
        return rejected(answer.client_id, Error.CLIENT_ID_CONFLICT)

    return {
        "clientId": answer.client_id,
        "status": Status.ALREADY_SYNCED,
        "correct": seen.was_correct,
        "correctIds": seen.question.correct_option_ids,
        "answeredAt": seen.answered_at,
    }
