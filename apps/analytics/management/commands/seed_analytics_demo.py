import random
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import User
from apps.billing.models import Order, OrderStatus
from apps.catalog.models import Package
from apps.core.models import PublishStatus
from apps.entitlements.models import Subscription
from apps.learning.models import (
    Chapter,
    EbookSectionProgress,
    ExamKind,
    HazardAttempt,
    HazardClip,
    LessonProgress,
    MediaAsset,
    MediaKind,
    MockAttempt,
    PracticeExam,
    Question,
    QuestionAttempt,
    QuestionOption,
    Subchapter,
)

LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1", "db"})

PACKAGES = (("Online Start", 30, "39.95"), ("Online Plus", 60, "44.95"), ("Online Premium", 90, "49.95"))

CHAPTERS = (
    "The driver",
    "Your vehicle",
    "Rules of the road",
    "Road signs",
    "Safety margins",
    "Hazard awareness",
    "Vulnerable road users",
    "Motorway rules",
)


class Command(BaseCommand):
    help = "Create demo students, orders, access periods and learning activity for the dashboard."

    def add_arguments(self, parser):
        parser.add_argument("--students", type=int, default=24, help="How many demo students to create.")
        parser.add_argument("--days", type=int, default=90, help="How far back the demo data goes.")

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError(
                "Refusing to run: this writes invented customers and payments, so it is for development only."
            )
        host = settings.DATABASES["default"].get("HOST") or ""
        if host not in LOCAL_HOSTS:
            raise CommandError(
                f"Refusing to run against the database at {host!r}: demo data belongs on a developer's "
                "own machine, not on a shared one."
            )

        random.seed(20261022)
        packages = self._packages()
        chapters, lessons, questions = self._course()
        exam = self._exam(questions)
        clips = self._clips()

        created = self._students(options["students"], options["days"], packages)
        self._learning(created, lessons, questions, exam, clips, options["days"])

        self.stdout.write(self.style.SUCCESS(f"Seeded {len(created)} students and their activity."))

    def _packages(self) -> list[Package]:
        out = []
        for order, (name, days, price) in enumerate(PACKAGES):
            package, _ = Package.objects.get_or_create(
                name=name,
                defaults={"duration_days": days, "price": Decimal(price), "display_order": order},
            )
            out.append(package)
        return out

    def _course(self):
        chapters, lessons, questions = [], [], []
        for position, title in enumerate(CHAPTERS, start=1):
            chapter, _ = Chapter.objects.get_or_create(
                slug=f"demo-{position}",
                defaults={"title": title, "order": position, "status": PublishStatus.PUBLISHED},
            )
            chapters.append(chapter)
            for index in range(1, 6):
                lesson, _ = Subchapter.objects.get_or_create(
                    slug=f"demo-{position}-{index}",
                    defaults={
                        "chapter": chapter,
                        "title": f"{title}, part {index}",
                        "order": index,
                        "status": PublishStatus.PUBLISHED,
                    },
                )
                lessons.append(lesson)
            for index in range(1, 7):
                question, made = Question.objects.get_or_create(
                    key=f"demo-q-{position}-{index}",
                    defaults={
                        "chapter": chapter,
                        "prompt": f"Demo question {index} about {title.lower()}?",
                        "status": PublishStatus.PUBLISHED,
                    },
                )
                if made:
                    QuestionOption.objects.bulk_create(
                        QuestionOption(
                            question=question, option_id=letter, text=f"Option {letter}", is_correct=letter == "a"
                        )
                        for letter in ("a", "b", "c", "d")
                    )
                questions.append(question)
        return chapters, lessons, questions

    def _exam(self, questions) -> PracticeExam:
        exam, made = PracticeExam.objects.get_or_create(
            slug="demo-mock",
            defaults={
                "title": "Demo mock test",
                "kind": ExamKind.MOCK,
                "pass_mark": 43,
                "status": PublishStatus.PUBLISHED,
            },
        )
        if made:
            for order, question in enumerate(questions[:50]):
                exam.exam_questions.create(question=question, order=order)
        return exam

    def _clips(self) -> list[HazardClip]:
        media = MediaAsset.objects.filter(title="Demo clip").first()
        if media is None:
            media = MediaAsset(title="Demo clip", kind=MediaKind.VIDEO)
            media.file.save("demo.mp4", ContentFile(b"demo clip"), save=False)
            media.save()
        clips = []
        for slug, title in (("demo-country-lane", "Country lane bend"), ("demo-highway", "Highway")):
            clip, made = HazardClip.objects.get_or_create(
                slug=slug, defaults={"title": title, "media": media, "status": PublishStatus.PUBLISHED}
            )
            if made:
                clip.windows.create(label="Demo hazard", starts_at=Decimal("4.00"), ends_at=Decimal("9.00"), order=0)
            clips.append(clip)
        return clips

    def _students(self, count: int, days: int, packages) -> list[User]:
        now = timezone.now()
        created = []
        for index in range(1, count + 1):
            joined = now - timedelta(days=random.randint(0, days), hours=random.randint(0, 23))
            student, made = User.objects.get_or_create(
                email=f"demo.student{index}@example.com",
                defaults={"first_name": "Demo", "last_name": f"Student {index}", "email_verified": True},
            )
            if not made:
                created.append(student)
                continue
            User.objects.filter(pk=student.pk).update(date_joined=joined)
            student.date_joined = joined
            created.append(student)

            if random.random() < 0.75:
                self._purchase(student, random.choice(packages), joined, now)
            elif random.random() < 0.5:
                self._unfinished(student, random.choice(packages), joined)
        return created

    def _purchase(self, student: User, package: Package, joined, now) -> None:
        paid_at = joined + timedelta(hours=random.randint(1, 72))
        if paid_at > now:
            paid_at = now
        order = Order.objects.create(
            user=student,
            package=package,
            email=student.email,
            status=OrderStatus.PAID,
            original_price=package.price,
            final_price=package.price,
            paid_at=paid_at,
        )
        Subscription.objects.create(
            user=student,
            order=order,
            package=package,
            starts_at=paid_at,
            package_expires_at=paid_at + timedelta(days=package.duration_days),
            account_expires_at=paid_at + timedelta(days=int(package.duration_days * 1.5)),
        )
        if random.random() < 0.1:
            Order.objects.filter(pk=order.pk).update(
                status=OrderStatus.REFUNDED, refunded_at=paid_at + timedelta(days=3)
            )

    def _unfinished(self, student: User, package: Package, joined) -> None:
        Order.objects.create(
            user=student,
            package=package,
            email=student.email,
            status=random.choice([OrderStatus.PENDING, OrderStatus.FAILED]),
            original_price=package.price,
            final_price=package.price,
        )

    def _learning(self, studentss, lessons, questions, exam, clips, days) -> None:
        now = timezone.now()
        for student in studentss:
            if random.random() < 0.25:
                continue

            for lesson in random.sample(lessons, random.randint(1, 12)):
                progress, made = LessonProgress.objects.get_or_create(student=student, subchapter=lesson)
                if made:
                    LessonProgress.objects.filter(pk=progress.pk).update(
                        completed_at=now - timedelta(days=random.randint(0, days))
                    )

            for question in random.sample(questions, random.randint(5, 30)):
                attempt = QuestionAttempt.objects.create(
                    student=student, question=question, was_correct=random.random() < 0.65
                )
                QuestionAttempt.objects.filter(pk=attempt.pk).update(
                    created_at=now - timedelta(days=random.randint(0, days))
                )

            for clip in clips:
                if random.random() < 0.6:
                    hazard = HazardAttempt.objects.create(
                        student=student, clip=clip, score=random.randint(0, 5), top_score=5
                    )
                    HazardAttempt.objects.filter(pk=hazard.pk).update(
                        created_at=now - timedelta(days=random.randint(0, days))
                    )

            if random.random() < 0.5:
                asked = random.sample(questions, min(50, len(questions)))
                answers = [{"id": question.pk, "correct": random.random() < 0.6} for question in asked]
                score = sum(1 for answer in answers if answer["correct"])
                mock = MockAttempt.objects.create(
                    student=student,
                    exam=exam,
                    score=score,
                    total=len(answers),
                    passed=score >= exam.pass_mark,
                    result={"questions": answers},
                )
                MockAttempt.objects.filter(pk=mock.pk).update(created_at=now - timedelta(days=random.randint(0, days)))

            for section in self._sections():
                if random.random() < 0.4:
                    EbookSectionProgress.objects.get_or_create(student=student, section=section)

    def _sections(self):
        from apps.learning.models import EbookSection  # noqa: PLC0415 - only used here

        return list(EbookSection.objects.filter(status=PublishStatus.PUBLISHED)[:10])
