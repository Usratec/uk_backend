from datetime import timedelta

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.billing.models import Order, OrderStatus
from apps.core.models import PublishStatus
from apps.entitlements import services as entitlements
from apps.entitlements.models import Subscription
from apps.learning import offline, services
from apps.learning.models import Chapter, Question, QuestionAttempt, QuestionOption
from apps.learning.tests.conftest import sign_in

pytestmark = pytest.mark.django_db

PACK_URL = reverse("learn-offline-practice")
BATCH_URL = reverse("learn-answers-batch")
ANSWER_URL = reverse("learn-answer")
PRACTICE_URL = reverse("learn-practice")
PROGRESS_URL = reverse("learn-progress")


def days_ago(count: int):
    now = timezone.now()
    return min(now.replace(hour=12, minute=0, second=0, microsecond=0) - timedelta(days=count), now)


@pytest.fixture
def live_question(make_question, chapter, publisher):
    chapter.status = PublishStatus.PUBLISHED
    chapter.save(update_fields=["status"])
    question = make_question()
    services.publish(actor=publisher, instance=question)
    question.refresh_from_db()
    return question


@pytest.fixture
def studying(student):
    def buy(package_name="starter"):
        learner = student(package_name)
        Subscription.objects.filter(user=learner).update(
            starts_at=days_ago(90), package_expires_at=timezone.now() + timedelta(days=30)
        )
        return learner

    return buy


def item(question, client_id="offline-1", selected=("a",), answered_at=None) -> dict:
    return {
        "clientId": client_id,
        "questionId": question.pk,
        "selected": list(selected),
        "answeredAt": (answered_at or timezone.now()).isoformat(),
    }


def sync(api, learner, *items):
    return sign_in(api, learner).post(BATCH_URL, items, format="json")


def results_by_id(response) -> dict:
    return {result["clientId"]: result for result in response.json()["results"]}


def error_code(response, client_id="offline-1") -> str:
    return results_by_id(response)[client_id]["error"]["code"]


def publish_question(chapter, publisher, key: str) -> Question:
    question = Question.objects.create(chapter=chapter, key=key, prompt="Which of these?")
    QuestionOption.objects.create(question=question, option_id="a", text="Yes", is_correct=True)
    QuestionOption.objects.create(question=question, option_id="b", text="No")
    services.publish(actor=publisher, instance=question)
    return question


def period(learner, starts_at, days: int, package) -> Subscription:
    paid = {"status": OrderStatus.PAID, "original_price": package.price, "final_price": package.price}
    order = Order.objects.create(package=package, email=learner.email, user=learner, paid_at=starts_at, **paid)
    return Subscription.objects.create(
        user=learner,
        order=order,
        package=package,
        starts_at=starts_at,
        package_expires_at=starts_at + timedelta(days=days),
        account_expires_at=starts_at + timedelta(days=days * 2),
    )


class TestThePack:
    def test_a_question_carries_what_is_needed_to_ask_and_mark_it(self, api, student, live_question, sign):
        live_question.media_sign = sign
        live_question.learn_more = live_question.chapter.subchapters.create(slug="reading", title="Further reading")
        live_question.explanation = "Because it takes your eyes off the road."
        live_question.save()
        live_question.options.filter(option_id="b").update(sign=sign)

        body = sign_in(api, student()).get(PACK_URL).json()
        question = body["questions"][0]

        assert question["correctIds"] == ["a"]
        assert question["explanation"] == "Because it takes your eyes off the road."
        assert question["lesson"] == {"slug": "reading", "title": "Further reading"}
        assert ([option["id"] for option in question["options"]], question["type"]) == (list("abcd"), "single")
        assert body["signs"][sign.code]["spec"] == sign.spec

    def test_the_pack_is_every_question_practice_could_ask_and_nothing_else(
        self, api, student, live_question, packages, make_question, publisher
    ):
        second = make_question(key="second")
        services.publish(actor=publisher, instance=second)
        make_question(key="draft")
        theirs = Chapter.objects.create(slug="premium-only", title="Premium only", status=PublishStatus.PUBLISHED)
        theirs.only_for_packages.add(packages["premium"])
        services.publish(actor=publisher, instance=make_question(key="premium-q", chapter=theirs))

        body = sign_in(api, student("starter")).get(PACK_URL).json()

        assert body["total"] == 2
        assert body["nextAfter"] is None
        assert [question["id"] for question in body["questions"]] == sorted([live_question.pk, second.pk])

    def test_the_answers_appear_in_the_pack_and_nowhere_else(self, api, student, live_exam):
        learner = student()

        pack = sign_in(api, learner).get(PACK_URL).json()
        exam = sign_in(api, learner).get(reverse("learn-exam", args=[live_exam.slug])).json()
        practice = sign_in(api, learner).get(PRACTICE_URL).json()

        assert all(question["correctIds"] for question in pack["questions"])
        for response in (exam, practice):
            assert response["questions"]
            for question in response["questions"]:
                assert "correctIds" not in question
                assert all("is_correct" not in option for option in question["options"])


class TestVersioning:
    def version(self, api, learner) -> str:
        return sign_in(api, learner).get(PACK_URL).json()["version"]

    def test_the_version_follows_answers_and_question_count(self, api, student, live_question, make_question):
        learner = student()
        second = make_question(key="second")
        services.publish(actor=live_question.published_by, instance=second)
        before = self.version(api, learner)

        option = live_question.options.get(option_id="b")
        option.is_correct = True
        option.save()
        reworked = self.version(api, learner)

        second.status = PublishStatus.DRAFT
        second.save(update_fields=["status"])

        assert reworked != before
        assert self.version(api, learner) not in (before, reworked)


class TestPaging:
    @pytest.fixture
    def many(self, chapter, publisher):
        chapter.status = PublishStatus.PUBLISHED
        chapter.save(update_fields=["status"])
        return [publish_question(chapter, publisher, f"q{number}") for number in range(7)]

    def test_following_the_cursor_reaches_every_question(self, api, student, many):
        learner = student()
        seen, after, pages = [], None, 0

        while True:
            query = f"?limit=3&after={after}" if after else "?limit=3"
            body = sign_in(api, learner).get(PACK_URL + query).json()
            seen += [question["id"] for question in body["questions"]]
            pages += 1
            after = body["nextAfter"]
            if after is None:
                break

        assert seen == sorted(question.pk for question in many)
        assert (pages, body["total"]) == (3, 7)

    def test_asking_the_pack_does_not_cost_a_query_per_question(self, api, student, many, chapter, publisher):
        sign_in(api, student())

        with CaptureQueriesContext(connection) as small:
            api.get(PACK_URL)

        for number in range(7, 30):
            publish_question(chapter, publisher, f"extra{number}")

        with CaptureQueriesContext(connection) as large:
            response = api.get(PACK_URL)

        assert response.json()["total"] == 30
        assert len(large) == len(small)


class TestMarking:
    def test_the_server_decides_correctness_not_the_client(self, api, student, live_question):
        learner = student()
        right = item(live_question, "right", ["a"])
        wrong = item(live_question, "wrong", ["b"]) | {"correct": True}

        results = results_by_id(sync(api, learner, right, wrong))

        assert [results[name]["status"] for name in ("right", "wrong")] == ["synced", "synced"]
        assert (results["right"]["correct"], results["wrong"]["correct"]) == (True, False)
        assert results["wrong"]["correctIds"] == ["a"]
        assert QuestionAttempt.objects.filter(student=learner, was_correct=False).count() == 1


class TestTheStudyDate:
    def test_answers_count_towards_their_own_day(self, api, studying, live_question, make_question, publisher):
        learner = studying()
        second = make_question(key="second")
        services.publish(actor=publisher, instance=second)
        when = days_ago(3)

        sync(
            api, learner, item(live_question, "a1", ["a"], when), item(second, "a2", ["a"], when + timedelta(hours=1))
        )

        attempt = QuestionAttempt.objects.get(student=learner, question=live_question)
        assert attempt.answered_at == when
        assert attempt.created_at > when
        assert services.study_days(learner) == [when.astimezone(timezone.get_current_timezone()).date()]
        assert sign_in(api, learner).get(PROGRESS_URL).json()["questionsAnswered"] == 2

    def test_a_streak_is_the_same_whichever_order_the_days_arrive_in(self, api, studying, live_question):
        learner = studying()

        for day in (2, 0, 1):
            sync(api, learner, item(live_question, f"d{day}", ["a"], days_ago(day)))

        assert services.streak(learner) == 3

    def test_a_historical_mistake_lasts_until_a_later_day_puts_it_right(self, api, studying, live_question):
        learner = studying()

        sync(api, learner, item(live_question, "old", ["b"], days_ago(5)))
        assert services.question_ids_answered_wrong(learner) == {live_question.pk}

        sync(api, learner, item(live_question, "new", ["a"], days_ago(1)))
        assert services.question_ids_answered_wrong(learner) == set()

    def test_a_historical_right_answer_counts_towards_mastery(self, api, studying, live_question):
        learner = studying()

        sync(api, learner, item(live_question, selected=["a"], answered_at=days_ago(6)))

        summary = sign_in(api, learner).get(PROGRESS_URL).json()
        assert (summary["questionsLearnt"], summary["mastery"]) == (1, 100)
        assert sign_in(api, learner).get(PRACTICE_URL + "?mode=weak").json()["questions"] == []


class TestValidation:
    def test_a_timestamp_the_server_cannot_trust_is_refused(self, api, studying, live_question):
        learner = studying()
        future = item(live_question, "future", ["a"], timezone.now() + timedelta(days=1))
        too_old = item(live_question, "old", ["a"], timezone.now() - offline.MAX_BACKDATE - timedelta(days=1))
        naive = item(live_question, "naive") | {"answeredAt": "2026-11-02T14:30:00"}

        results = results_by_id(sync(api, learner, future, too_old, naive))

        assert results["future"]["error"]["code"] == "answered_at_in_future"
        assert results["old"]["error"]["code"] == "answered_at_too_old"
        assert results["naive"]["error"]["code"] == "answered_at_not_timezone_aware"
        assert not QuestionAttempt.objects.filter(student=learner).exists()

    def test_a_question_practice_could_not_ask_is_refused(self, api, student, live_question, make_question):
        learner = student()
        draft = make_question(key="draft")
        unknown = item(live_question) | {"questionId": live_question.pk + 9999}

        assert error_code(sync(api, learner, unknown)) == "question_not_found"
        assert error_code(sync(api, learner, item(draft))) == "question_not_found"
        assert not QuestionAttempt.objects.filter(student=learner).exists()

    def test_a_selection_the_question_does_not_offer_is_refused(self, api, student, live_question):
        learner = student()

        unknown = results_by_id(sync(api, learner, item(live_question, selected=["z"])))["offline-1"]
        repeated = results_by_id(sync(api, learner, item(live_question, selected=["a", "a"])))["offline-1"]

        assert unknown["error"]["code"] == "invalid_selection"
        assert unknown["error"]["detail"] == ["a", "b", "c", "d"]
        assert repeated["error"]["code"] == "invalid_selection"

    def test_valid_answers_survive_a_batch_of_bad_ones(self, api, student, live_question):
        learner = student()
        future = item(live_question, "future", ["a"], timezone.now() + timedelta(days=2))
        good = item(live_question, "good")

        results = results_by_id(sync(api, learner, future, {"clientId": "broken"}, good, {"nonsense": True}))

        assert results["good"]["status"] == "synced"
        assert results["future"]["status"] == "rejected"
        assert results["broken"]["error"]["code"] == "malformed"
        assert results["broken"]["error"]["detail"].keys() >= {"questionId", "selected", "answeredAt"}
        assert QuestionAttempt.objects.filter(student=learner).count() == 1

    def test_a_batch_the_endpoint_cannot_accept_is_refused_whole(self, api, student, live_question):
        learner = student()
        many = [item(live_question, f"c{n}") for n in range(offline.MAX_BATCH_ITEMS + 1)]

        assert sync(api, learner, *many).status_code == 400
        assert sign_in(api, learner).post(BATCH_URL, {"not": "a list"}, format="json").status_code == 400
        assert not QuestionAttempt.objects.filter(student=learner).exists()

    def test_a_learner_without_a_plan_is_asked_to_pay(self, api, outsider, live_question):
        pack = sign_in(api, outsider).get(PACK_URL)

        assert (pack.status_code, sync(api, outsider, item(live_question)).status_code) == (402, 402)
        assert pack.json()["detail"]


class TestHistoricalAccess:
    def test_only_an_unrevoked_period_pays_and_only_for_its_own_days(self, api, student, live_question, packages):
        learner = student("starter")
        Subscription.objects.filter(user=learner).delete()
        period(learner, days_ago(28), days=14, package=packages["starter"])
        period(learner, days_ago(10), days=30, package=packages["starter"])
        revoked = period(learner, days_ago(13), days=2, package=packages["starter"])
        revoked.revoked_at = timezone.now()
        revoked.save(update_fields=["revoked_at"])

        sent = [
            item(live_question, "covered", ["a"], days_ago(20)),
            item(live_question, "revoked", ["a"], days_ago(12)),
        ]
        results = results_by_id(sync(api, learner, *sent, item(live_question, "before", ["a"], days_ago(29))))

        assert results["covered"]["status"] == "synced"
        assert results["revoked"]["error"]["code"] == "no_access_on_that_date"
        assert results["before"]["error"]["code"] == "no_access_on_that_date"
        assert QuestionAttempt.objects.filter(student=learner).count() == 1

    def test_the_moment_a_period_starts_counts_and_the_moment_it_expires_does_not(
        self, api, student, live_question, packages
    ):
        learner = student("starter")
        started = days_ago(20)
        bought = period(learner, started, days=10, package=packages["starter"])
        start = item(live_question, "start", ["a"], started)
        expiry = item(live_question, "expiry", ["a"], bought.package_expires_at)

        results = results_by_id(sync(api, learner, start, expiry))

        assert results["start"]["status"] == "synced"
        assert results["expiry"]["error"]["code"] == "no_access_on_that_date"

    def test_a_question_the_package_of_the_day_did_not_include_is_refused(
        self, api, student, packages, make_question, publisher
    ):
        premium_only = Chapter.objects.create(
            slug="premium-only", title="Premium only", status=PublishStatus.PUBLISHED
        )
        premium_only.only_for_packages.add(packages["premium"])
        question = make_question(key="premium-q", chapter=premium_only)
        services.publish(actor=publisher, instance=question)
        learner = student("premium")
        Subscription.objects.filter(user=learner).update(starts_at=days_ago(2))
        starter = period(learner, days_ago(28), days=20, package=packages["starter"])
        then = item(question, "then", ["a"], starter.starts_at + timedelta(days=2))

        results = results_by_id(sync(api, learner, then, item(question, "now")))

        assert results["then"]["error"]["code"] == "no_access_on_that_date"
        assert results["now"]["status"] == "synced"


class TestIdempotency:
    def test_sending_the_same_batch_twice_records_it_once(self, api, studying, live_question):
        learner = studying()
        batch = [item(live_question, "a1", ["a"], days_ago(2)), item(live_question, "a2", ["b"], days_ago(2))]

        first = results_by_id(sign_in(api, learner).post(BATCH_URL, batch, format="json"))
        before = sign_in(api, learner).get(PROGRESS_URL).json()
        second = results_by_id(sign_in(api, learner).post(BATCH_URL, batch, format="json"))
        after = sign_in(api, learner).get(PROGRESS_URL).json()

        assert {result["status"] for result in first.values()} == {"synced"}
        assert {result["status"] for result in second.values()} == {"already_synced"}
        assert (second["a2"]["correct"], second["a2"]["correctIds"]) == (False, ["a"])
        assert QuestionAttempt.objects.filter(student=learner).count() == 2
        assert before == after

    def test_reusing_an_id_for_anything_different_is_refused(
        self, api, studying, live_question, make_question, publisher
    ):
        learner = studying()
        second = make_question(key="second")
        services.publish(actor=publisher, instance=second)
        when = days_ago(1)
        for client_id in ("a", "b", "c"):
            sync(api, learner, item(live_question, client_id, ["a"], when))

        changed = [item(second, "a", ["a"], when), item(live_question, "b", ["b"], when)]
        results = results_by_id(sync(api, learner, *changed, item(live_question, "c", ["a"], days_ago(2))))

        assert [results[client_id]["error"]["code"] for client_id in "abc"] == ["client_id_conflict"] * 3
        assert QuestionAttempt.objects.filter(student=learner).count() == 3

    def test_a_row_appearing_after_the_lookup_is_not_counted_twice(self, monkeypatch, studying, live_question):
        learner = studying()
        answer = offline.Answer("raced", live_question.pk, ["a"], days_ago(1))
        periods = entitlements.access_periods

        def another_request_gets_there_first(user, feature=None):
            QuestionAttempt.objects.create(
                student=learner,
                question=live_question,
                was_correct=True,
                answered_at=answer.answered_at,
                client_id="raced",
                client_payload=offline.digest_of(answer),
            )
            monkeypatch.setattr(entitlements, "access_periods", periods)
            return periods(user, feature)

        monkeypatch.setattr(entitlements, "access_periods", another_request_gets_there_first)

        [result] = offline.sync(learner, [answer])

        assert (result["status"], result["correct"]) == ("already_synced", True)
        assert QuestionAttempt.objects.filter(student=learner).count() == 1


class TestTheOnlineEndpointIsUnchanged:
    def test_answering_online_still_works_and_records_today(self, api, student, live_question):
        learner = student()
        answer = {"questionId": live_question.pk, "selected": ["a"]}

        body = sign_in(api, learner).post(ANSWER_URL, answer, format="json").json()
        for _ in range(2):
            sign_in(api, learner).post(ANSWER_URL, answer, format="json")

        assert body["correct"] is True
        assert body["correctIds"] == ["a"]
        assert set(body) == {"correct", "correctIds", "explanation", "lesson"}

        attempts = QuestionAttempt.objects.filter(student=learner, client_id="")
        assert attempts.count() == 3
        assert QuestionAttempt.objects.get(pk=attempts[0].pk).answered_at.date() == timezone.localdate()
