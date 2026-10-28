"""The Learning screens.

Screens only: which material may be published, whether it is fit to publish, and who may see it
are decided in ``services.py`` and ``apps.entitlements``, so the same rules apply to the student
API.

The hierarchy is navigated the way staff think about it — a chapter lists its subchapters, a
subchapter lists its material — while each item is written on its own page, because a rich-text
editor inside a nested inline is unusable.
"""

from typing import cast

from django import forms
from django.contrib import admin, messages
from django.core.files.uploadedfile import UploadedFile
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import URLPattern, path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString, mark_safe
from django_ckeditor_5.widgets import CKEditor5Widget  # type: ignore[import-untyped]  # ships no types

from apps.accounts.models import User
from apps.core.models import PublishStatus
from apps.learning import html, services
from apps.learning.models import (
    AVAILABLE_CONTENT_TYPES,
    Chapter,
    ContentType,
    Ebook,
    EbookSection,
    EbookSectionProgress,
    ExamHazardClip,
    ExamKind,
    ExamQuestion,
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
    QuestionOption,
    SavedQuestion,
    Sign,
    Subchapter,
    Subtitle,
    VideoProgress,
    default_subtitle_language,
)
from apps.staff.admin import PermissionRequiredAdminMixin, badge, placeholder

STATUS_TONES = {PublishStatus.PUBLISHED: "success", PublishStatus.DRAFT: "neutral"}


class ContentForm(forms.ModelForm):
    """Writes the material in a rich-text editor.

    Whatever the editor produces is cleaned when the model is saved, so the toolbar is a
    convenience rather than the thing that keeps the content safe.
    """

    body_html = forms.CharField(
        label="Content",
        required=False,
        widget=CKEditor5Widget(config_name="default"),
    )

    class Meta:
        model = LearningContent
        # Listed rather than "__all__", so a field added later is not exposed by accident.
        # Status is not here: publishing goes through the service, not the form.
        fields = [
            "subchapter",
            "content_type",
            "title",
            "slug",
            "body_html",
            "question",
            "media",
            "hazard_clip",
            "order",
            "only_for_packages",
            "publish_from",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Only the kinds of material the product can serve today. The rest exist in the model so
        # the hierarchy does not change when they arrive.
        content_type = self.fields["content_type"]
        content_type.choices = [  # type: ignore[attr-defined]  # it is a ChoiceField here
            (value, label) for value, label in ContentType.choices if value in AVAILABLE_CONTENT_TYPES
        ]


class PublishableAdmin(PermissionRequiredAdminMixin, admin.ModelAdmin):
    """Shared behaviour for anything students only see once it is published.

    Gated on permissions that were actually granted, as the rest of the back office already is:
    being a superuser is not by itself a way into the course material, and the whole Learning
    section stays out of the menu until somebody is given the Online learning materials role.
    Someone who administers the system can still grant themselves that role — least privilege by
    default, not a wall.
    """

    actions = ["publish_selected", "unpublish_selected"]

    @admin.display(description="Status", ordering="status")
    def state(self, obj) -> SafeString:
        if obj.is_published and not obj.is_live():
            return badge("Scheduled", "neutral")
        return badge(obj.get_status_display(), STATUS_TONES.get(obj.status, "neutral"))

    @admin.display(description="Ready to publish")
    def readiness(self, obj) -> SafeString:
        """Tells an editor what is still missing, rather than only refusing at publish time."""
        if obj.pk is None:
            return placeholder()
        problems = services.problems_with(obj)
        warnings = services.warnings_about(obj)

        if problems:
            listed = format_html_join(mark_safe("<br>"), "&bull; {}", ((problem,) for problem in problems))
            return format_html("Not yet:<br>{}", listed)

        if warnings:
            # Publishing is allowed; this is worth knowing rather than worth stopping for.
            listed = format_html_join(mark_safe("<br>"), "&bull; {}", ((warning,) for warning in warnings))
            return format_html("{}<br>{}", badge("Ready", "success"), listed)

        return badge("Ready", "success")

    def has_publish_permission(self, request: HttpRequest) -> bool:
        return request.user.has_perm(f"{self.opts.app_label}.publish_{self.opts.model_name}")

    @admin.action(description="Publish the selected material")
    def publish_selected(self, request: HttpRequest, queryset) -> None:
        published = 0
        for instance in queryset:
            try:
                services.publish(actor=request.user, instance=instance)
                published += 1
            except services.NotReadyToPublishError as error:
                self.message_user(request, f"{instance}: {error}", level=messages.ERROR)
            except Exception:
                self.message_user(request, f"You may not publish {instance}.", level=messages.ERROR)
        if published:
            self.message_user(request, f"Published {published} item(s).", level=messages.SUCCESS)

    @admin.action(description="Take the selected material off the site")
    def unpublish_selected(self, request: HttpRequest, queryset) -> None:
        for instance in queryset:
            try:
                services.unpublish(actor=request.user, instance=instance)
            except Exception:
                self.message_user(request, f"You may not unpublish {instance}.", level=messages.ERROR)

    def get_actions(self, request: HttpRequest):
        """Hide the publish actions from staff who may not use them."""
        actions = super().get_actions(request)
        if not self.has_publish_permission(request):
            actions.pop("publish_selected", None)
            actions.pop("unpublish_selected", None)
        return actions


def _thumbnail(image, width: int) -> SafeString:
    if not image:
        return placeholder()
    return format_html('<img src="{}" style="max-width:{}px;height:auto;border-radius:4px" alt="">', image.url, width)


def _hazard_sheet(instance) -> dict | None:
    """The clip and its hazard timings, for a clip or for a content item showing one."""
    clip = instance if isinstance(instance, HazardClip) else getattr(instance, "hazard_clip", None)
    return services.hazard_sheet_for(clip) if clip else None


def _answer_sheet(instance) -> dict | None:
    """The answers to check, for a question or for a content item that asks one."""
    question = instance if isinstance(instance, Question) else getattr(instance, "question", None)
    return services.answer_sheet_for(question) if question else None


def _ebook_sheet(instance) -> dict | None:
    return services.ebook_sheet_for(instance) if isinstance(instance, Ebook) else None


class PreviewMixin(admin.ModelAdmin):
    """Adds a page showing material as a student would see it.

    Declared on top of ModelAdmin, because it is only ever mixed into one and relies on its urls,
    permissions and model.
    """

    preview_template = "admin/learning/preview.html"

    def get_urls(self) -> list[URLPattern]:
        meta = self.model._meta
        preview = path(
            "<int:object_id>/preview/",
            self.admin_site.admin_view(self.preview_view),
            name=f"{meta.app_label}_{meta.model_name}_preview",
        )
        return [preview, *super().get_urls()]

    def preview_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        """Render the material for staff who may view it. Checked here, not just hidden in the UI."""
        if not self.has_view_permission(request):
            return HttpResponse("You may not preview this material.", status=403)

        instance = get_object_or_404(self.model, pk=object_id)
        return render(
            request,
            self.preview_template,
            {
                **self.admin_site.each_context(request),
                "title": f"Preview: {instance}",
                "instance": instance,
                "content": services.student_view_of(instance),
                "problems": services.problems_with(instance),
                # A question, or a content item that asks one; the template leaves the section
                # out for anything else.
                "answer_sheet": _answer_sheet(instance),
                # A hazard clip, or a content item showing one.
                "hazard_sheet": _hazard_sheet(instance),
                # An e-book, which has a file to open rather than a body to read.
                "ebook_sheet": _ebook_sheet(instance),
                "opts": self.model._meta,
            },
        )

    @admin.display(description="Preview")
    def preview_link(self, obj) -> SafeString:
        if obj.pk is None:
            return placeholder()
        meta = self.model._meta
        url = reverse(f"admin:{meta.app_label}_{meta.model_name}_preview", args=[obj.pk])
        return format_html('<a class="adm-btn" href="{}" target="_blank">See it as a student</a>', url)


# --- Inlines, for navigating the hierarchy -------------------------------------------------------


class ChildInline(admin.TabularInline):
    """Lists what sits under this record, for ordering and getting to it.

    Each child is written on its own page: a rich-text editor inside a nested inline is unusable,
    and the publishing and access settings would not fit either.
    """

    extra = 0
    show_change_link = True

    @admin.display(description="Status")
    def state(self, obj) -> SafeString:
        if obj.pk is None:
            return placeholder()
        if obj.is_published and not obj.is_live():
            return badge("Scheduled", "neutral")
        return badge(obj.get_status_display(), STATUS_TONES.get(obj.status, "neutral"))


class SubchapterInline(ChildInline):
    model = Subchapter
    fields = ["title", "order", "state"]
    readonly_fields = ["state"]
    ordering = ["order"]


class LearningContentInline(ChildInline):
    model = LearningContent
    fields = ["title", "content_type", "order", "state"]
    readonly_fields = ["state"]
    ordering = ["order"]
    verbose_name_plural = "Learning content"


# --- The screens ---------------------------------------------------------------------------------


@admin.register(Chapter)
class ChapterAdmin(PublishableAdmin):
    list_display = ["title", "order", "subchapter_count", "state"]
    list_editable = ["order"]
    list_filter = ["status"]
    search_fields = ["title", "slug"]
    prepopulated_fields = {"slug": ("title",)}
    inlines = [SubchapterInline]
    fieldsets = (
        (None, {"fields": ("title", "slug", "description", "icon", "order")}),
        (
            "Publishing",
            {
                "fields": ("status", "publish_from", "published_at", "published_by", "readiness"),
                "description": "Students see this only once it is published.",
            },
        ),
    )
    readonly_fields = ["status", "published_at", "published_by", "readiness"]

    @admin.display(description="Subchapters")
    def subchapter_count(self, chapter: Chapter) -> str:
        published = chapter.subchapters.filter(status=PublishStatus.PUBLISHED).count()
        return f"{published} published / {chapter.subchapters.count()} total"


@admin.register(Subchapter)
class SubchapterAdmin(PreviewMixin, PublishableAdmin):
    list_display = ["title", "chapter", "order", "content_count", "state"]
    list_editable = ["order"]
    list_filter = ["status", "chapter"]
    search_fields = ["title", "slug", "summary"]
    prepopulated_fields = {"slug": ("title",)}
    list_select_related = ["chapter"]
    autocomplete_fields = ["chapter"]
    inlines = [LearningContentInline]
    fieldsets = (
        (None, {"fields": ("chapter", "title", "slug", "summary", "minutes", "order")}),
        (
            "Publishing",
            {
                "fields": (
                    "status",
                    "publish_from",
                    "published_at",
                    "published_by",
                    "readiness",
                    "preview_link",
                )
            },
        ),
    )
    readonly_fields = ["status", "published_at", "published_by", "readiness", "preview_link"]

    @admin.display(description="Material")
    def content_count(self, subchapter: Subchapter) -> str:
        published = subchapter.content.filter(status=PublishStatus.PUBLISHED).count()
        return f"{published} published / {subchapter.content.count()} total"


@admin.register(LearningContent)
class LearningContentAdmin(PreviewMixin, PublishableAdmin):
    form = ContentForm
    list_display = ["title", "subchapter", "chapter", "content_type", "order", "state"]
    list_editable = ["order"]
    list_filter = ["status", "content_type", "subchapter__chapter"]
    search_fields = ["title", "slug"]
    prepopulated_fields = {"slug": ("title",)}
    list_select_related = ["subchapter", "subchapter__chapter"]
    autocomplete_fields = ["subchapter", "question", "media", "hazard_clip"]
    filter_horizontal = ["only_for_packages"]
    fieldsets = (
        (None, {"fields": ("subchapter", "content_type", "title", "slug", "order")}),
        (
            "Content",
            {
                "fields": ("body_html", "question", "media", "hazard_clip"),
                "description": (
                    "Theory uses the text, question material uses the question, video and "
                    "document material use a file from the media library, and hazard material "
                    "uses a clip."
                ),
            },
        ),
        (
            "Access",
            {
                "fields": ("only_for_packages",),
                "description": "Who can see this. Leave empty for everyone with learning access.",
            },
        ),
        (
            "Publishing",
            {
                "fields": (
                    "status",
                    "publish_from",
                    "published_at",
                    "published_by",
                    "readiness",
                    "preview_link",
                )
            },
        ),
    )
    readonly_fields = ["status", "published_at", "published_by", "readiness", "preview_link"]

    @admin.display(description="Chapter", ordering="subchapter__chapter__order")
    def chapter(self, content: LearningContent) -> str:
        return content.subchapter.chapter.title


@admin.register(Sign)
class SignAdmin(PermissionRequiredAdminMixin, admin.ModelAdmin):
    """Road signs. Stored as drawing instructions, so there is no image to upload."""

    list_display = ["name", "code", "category", "order"]
    list_editable = ["order"]
    list_filter = ["category"]
    search_fields = ["code", "name", "meaning"]
    prepopulated_fields = {"code": ("name",)}


class ImageUploadForm(forms.ModelForm):
    image_field: str

    upload_image = forms.ImageField(
        label="Upload a picture",
        required=False,
        widget=forms.ClearableFileInput(attrs={"accept": "image/jpeg,image/png,image/webp,image/gif"}),
        help_text="JPG, PNG, WebP or GIF. It is added to the media library and chosen here.",
    )

    def clean_upload_image(self):
        upload = self.cleaned_data.get("upload_image")
        if upload:
            try:
                services.check_image_upload(upload)
            except services.ImageUploadError as error:
                raise forms.ValidationError(str(error)) from error
        return upload


def _attach_uploaded_image(request: HttpRequest, form: ImageUploadForm) -> None:
    upload = getattr(form, "cleaned_data", {}).get("upload_image")
    if not upload:
        return
    asset = services.image_asset_from_upload(upload=upload, uploaded_by=cast(User, request.user))
    setattr(form.instance, form.image_field, asset)


class QuestionOptionForm(ImageUploadForm):
    image_field = "image"

    class Meta:
        model = QuestionOption
        fields = ["order", "option_id", "text", "sign", "image", "is_correct"]


class QuestionOptionInline(admin.TabularInline):
    """The answers to choose between, edited beside the question they belong to.

    Each answer carries its own picture and its own upload box, so the four pictures of an image
    question are four uploads in one save rather than four trips to the media library.
    """

    model = QuestionOption
    form = QuestionOptionForm
    extra = 2
    fields = ["order", "option_id", "text", "sign", "image", "upload_image", "thumbnail", "is_correct"]
    readonly_fields = ["thumbnail"]
    ordering = ["order"]
    autocomplete_fields = ["sign", "image"]

    @admin.display(description="Picture")
    def thumbnail(self, option: QuestionOption) -> SafeString:
        if option.pk is None or option.image is None:
            return placeholder()
        return _thumbnail(option.image.file, width=90)


class QuestionForm(ImageUploadForm):
    image_field = "media_image"

    class Meta:
        model = Question
        fields = [
            "chapter",
            "key",
            "question_type",
            "prompt",
            "media_sign",
            "media_image",
            "explanation",
            "learn_more",
            "publish_from",
        ]


@admin.register(Question)
class QuestionAdmin(PreviewMixin, PublishableAdmin):
    """The question bank.

    One question, used wherever it is needed. Editing it reaches every exam and subchapter that
    refers to it, which is the point of a bank rather than copies.
    """

    list_display = ["prompt_preview", "key", "chapter", "question_type", "answer_summary", "state"]
    list_filter = ["status", "question_type", "chapter"]
    search_fields = ["key", "prompt", "explanation"]
    prepopulated_fields = {"key": ("prompt",)}
    list_select_related = ["chapter"]
    autocomplete_fields = ["chapter", "media_sign", "media_image", "learn_more"]
    form = QuestionForm
    inlines = [QuestionOptionInline]
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "chapter",
                    "key",
                    "question_type",
                    "prompt",
                    "media_sign",
                    "media_image",
                    "upload_image",
                    "thumbnail",
                )
            },
        ),
        (
            "After answering",
            {
                "fields": ("explanation", "learn_more"),
                "description": "Shown once the student has committed to an answer.",
            },
        ),
        (
            "Publishing",
            {"fields": ("status", "publish_from", "published_at", "published_by", "readiness", "preview_link")},
        ),
    )
    readonly_fields = ["status", "published_at", "published_by", "readiness", "preview_link", "thumbnail"]

    @admin.display(description="Picture shown now")
    def thumbnail(self, question: Question) -> SafeString:
        if question.pk is None or question.media_image is None:
            return placeholder()
        return _thumbnail(question.media_image.file, width=240)

    def save_model(self, request: HttpRequest, obj: Question, form, change: bool) -> None:
        _attach_uploaded_image(request, form)
        super().save_model(request, obj, form, change)

    def save_formset(self, request: HttpRequest, form, formset, change: bool) -> None:
        if formset.model is QuestionOption:
            for option_form in formset.forms:
                if getattr(option_form, "cleaned_data", {}).get("DELETE"):
                    continue
                _attach_uploaded_image(request, option_form)
        super().save_formset(request, form, formset, change)

    @admin.display(description="Question", ordering="prompt")
    def prompt_preview(self, question: Question) -> str:
        return question.prompt[:70] + ("…" if len(question.prompt) > 70 else "")

    @admin.display(description="Answers")
    def answer_summary(self, question: Question) -> str:
        options = question.options.all()
        correct = sum(1 for option in options if option.is_correct)
        return f"{len(options)} options, {correct} correct"


def _kept(formset, field: str) -> list:
    kept = []
    for form in formset.forms:
        if not form.cleaned_data or form.cleaned_data.get("DELETE"):
            continue
        chosen = form.cleaned_data.get(field)
        if chosen is not None:
            kept.append(chosen)
    return kept


def _rows_submitted(data, prefix: str, field: str) -> int:
    rows = 0
    for index in range(int(data.get(f"{prefix}-TOTAL_FORMS") or 0)):
        if data.get(f"{prefix}-{index}-DELETE"):
            continue
        if data.get(f"{prefix}-{index}-{field}"):
            rows += 1
    return rows


QUESTIONS_PREFIX = "exam_questions"
CLIPS_PREFIX = "exam_clips"


class ExamQuestionInlineFormSet(forms.BaseInlineFormSet):
    def clean(self) -> None:
        super().clean()
        if self.instance.pass_mark is None and _kept(self, "question"):
            raise forms.ValidationError(
                "This paper asks questions, so it needs a pass mark. "
                "Leave the pass mark empty only on a paper of hazard clips alone."
            )


class ExamQuestionInline(admin.TabularInline):
    """Which questions an exam asks, and in what order.

    The questions themselves are not edited here: an exam points at the bank, so changing a
    question in one exam would change it everywhere.
    """

    model = ExamQuestion
    formset = ExamQuestionInlineFormSet
    extra = 1
    fields = ["order", "question"]
    ordering = ["order"]
    autocomplete_fields = ["question"]


class ExamHazardClipInlineFormSet(forms.BaseInlineFormSet):
    def clean(self) -> None:
        super().clean()
        wanted = self.instance.hazard_pass_mark
        if not wanted:
            return

        clips = _kept(self, "clip")
        available = sum(clip.top_score for clip in clips)
        if wanted > available:
            raise forms.ValidationError(
                f"The hazard perception pass mark is {wanted}, but these clips are only worth "
                f"{available} marks, so nobody could pass them however early every hazard was spotted."
            )


class ExamHazardClipInline(admin.TabularInline):
    model = ExamHazardClip
    formset = ExamHazardClipInlineFormSet
    extra = 1
    fields = ["order", "clip", "worth"]
    readonly_fields = ["worth"]
    ordering = ["order"]
    autocomplete_fields = ["clip"]
    verbose_name = "hazard clip"
    verbose_name_plural = "Hazard clips"

    @admin.display(description="Top score")
    def worth(self, place: ExamHazardClip) -> str:
        """What this clip adds to the sitting's hazard total, so staff can see the paper's worth."""
        if place.pk is None or place.clip_id is None:
            return placeholder()
        return f"{place.clip.top_score} marks"


class PracticeExamForm(forms.ModelForm):
    class Meta:
        model = PracticeExam
        fields = [
            "title",
            "slug",
            "description",
            "kind",
            "order",
            "pass_mark",
            "time_limit_seconds",
            "hazard_pass_mark",
            "publish_from",
        ]

    def clean(self) -> dict:
        super().clean()
        cleaned = self.cleaned_data

        if f"{QUESTIONS_PREFIX}-TOTAL_FORMS" not in self.data:
            return cleaned

        questions = _rows_submitted(self.data, QUESTIONS_PREFIX, "question")
        clips = _rows_submitted(self.data, CLIPS_PREFIX, "clip")

        if cleaned.get("kind") == ExamKind.MOCK:
            if not questions:
                self.add_error("kind", "A mock test stands in for the real test, so it needs questions too.")
            if not clips:
                self.add_error("kind", "A mock test stands in for the real test, so it needs hazard clips too.")
        elif not questions and not clips:
            raise forms.ValidationError(
                "A practice exam needs something to sit: add at least one question or one hazard clip."
            )

        return cleaned


@admin.register(PracticeExam)
class PracticeExamAdmin(PublishableAdmin):
    list_display = ["title", "kind", "question_count", "clip_count", "pass_mark", "order", "state"]
    list_editable = ["order"]
    list_filter = ["status", "kind"]
    search_fields = ["title", "slug", "description"]
    prepopulated_fields = {"slug": ("title",)}
    form = PracticeExamForm
    inlines = [ExamQuestionInline, ExamHazardClipInline]
    fieldsets = (
        (None, {"fields": ("title", "slug", "description", "kind", "order")}),
        (
            "Rules",
            {
                "fields": ("pass_mark", "time_limit_seconds", "hazard_worth", "hazard_pass_mark"),
                "description": (
                    "Sets the pass marks and time limit. Questions and hazard clips are scored "
                    "separately. Students must pass both to pass the test. If no hazard pass mark "
                    "is set, only the questions determine whether they pass. A practice exam may "
                    "be questions alone or clips alone — leave the pass mark empty on a paper of "
                    "clips alone. A mock test stands in for the real test, so it needs both."
                ),
            },
        ),
        (
            "Publishing",
            {"fields": ("status", "publish_from", "published_at", "published_by", "readiness")},
        ),
    )
    readonly_fields = ["status", "published_at", "published_by", "readiness", "hazard_worth"]

    @admin.display(description="Questions")
    def question_count(self, exam: PracticeExam) -> int:
        return exam.questions.count()

    @admin.display(description="Clips")
    def clip_count(self, exam: PracticeExam) -> int:
        return exam.hazard_clips.count()

    @admin.display(description="Hazard marks available")
    def hazard_worth(self, exam: PracticeExam) -> str:
        """What the clips add up to, which is what a student's hazard score is out of."""
        if exam.pk is None:
            return placeholder()
        clips = list(exam.hazard_clips.all())
        if not clips:
            return "No clips, so this is a question-only paper."
        return f"{sum(clip.top_score for clip in clips)} marks across {len(clips)} clip(s)"


# --- What students have done ---------------------------------------------------------------------
#
# Records of a person, not of the course, so they are read-only everywhere: staff look at them to
# answer "how is this student getting on", and nothing good comes of editing somebody's history.


class ReadOnlyAdmin(PermissionRequiredAdminMixin, admin.ModelAdmin):
    """Shows records without offering a way to change them.

    These are students' own work — attempts, progress, saved questions — so viewing them is
    granted on purpose too, exactly as it is for the material itself.
    """

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


@admin.register(QuestionAttempt)
class QuestionAttemptAdmin(ReadOnlyAdmin):
    list_display = ["student", "question", "was_correct", "created_at"]
    list_filter = ["was_correct", "question__chapter", "created_at"]
    search_fields = ["student__email", "question__key", "question__prompt"]
    list_select_related = ["student", "question"]
    date_hierarchy = "created_at"


@admin.register(MockAttempt)
class MockAttemptAdmin(ReadOnlyAdmin):
    list_display = ["student", "exam", "question_result", "hazard_result", "passed", "created_at"]
    list_filter = ["passed", "exam", "created_at"]
    search_fields = ["student__email", "exam__title"]
    list_select_related = ["student", "exam"]
    date_hierarchy = "created_at"
    fieldsets = (
        (None, {"fields": ("student", "exam", "created_at")}),
        (
            "Questions",
            {
                "fields": ("question_result", "breakdown", "time_taken"),
                "description": "The pass mark covers the questions. A blank answer counts as a wrong one.",
            },
        ),
        (
            "Hazard perception",
            {
                "fields": ("hazard_result", "hazard_verdict"),
                "description": "Scored separately from the questions, and never added to them.",
            },
        ),
        (
            "Outcome",
            {
                "fields": ("passed",),
                "description": (
                    "The test as a whole, as it was decided when the sitting was marked: both "
                    "parts must be passed, and clips with no pass mark decide nothing."
                ),
            },
        ),
    )
    readonly_fields = [
        "student",
        "exam",
        "created_at",
        "question_result",
        "breakdown",
        "time_taken",
        "passed",
        "hazard_result",
        "hazard_verdict",
    ]

    @admin.display(description="Questions")
    def question_result(self, attempt: MockAttempt) -> str:
        return f"{attempt.score}/{attempt.total}"

    @admin.display(description="Breakdown")
    def breakdown(self, attempt: MockAttempt) -> str:
        counts = services.attempt_statistics(attempt)
        return (
            f"{counts['correctQuestions']} right, "
            f"{counts['incorrectQuestions']} wrong, "
            f"{counts['unansweredQuestions']} left blank, "
            f"of {counts['totalQuestions']} asked"
        )

    @admin.display(description="Time taken")
    def time_taken(self, attempt: MockAttempt) -> str:
        if attempt.time_taken_seconds is None:
            return "—"
        minutes, seconds = divmod(attempt.time_taken_seconds, 60)
        return f"{minutes}m {seconds}s"

    @admin.display(description="Hazard score")
    def hazard_result(self, attempt: MockAttempt) -> str:
        counts = services.attempt_statistics(attempt)
        if not counts["hazardMaxScore"]:
            return "—"
        return f"{counts['hazardTotalScore']}/{counts['hazardMaxScore']}"

    @admin.display(description="Hazard perception", boolean=False)
    def hazard_verdict(self, attempt: MockAttempt) -> str:
        """Read from the marking as it was reported, not judged again against today's pass mark."""
        verdict = (attempt.result or {}).get("hazardPassed")
        if verdict is None:
            return "Not judged — no pass mark was set for the clips."
        return "Passed" if verdict else "Failed"


@admin.register(LessonProgress)
class LessonProgressAdmin(ReadOnlyAdmin):
    list_display = ["student", "subchapter", "completed_at"]
    list_filter = ["subchapter__chapter", "completed_at"]
    search_fields = ["student__email", "subchapter__title"]
    list_select_related = ["student", "subchapter"]
    date_hierarchy = "completed_at"


@admin.register(VideoProgress)
class VideoProgressAdmin(ReadOnlyAdmin):
    list_display = ["student", "content", "lesson", "position_seconds", "duration_seconds", "updated_at"]
    list_filter = ["content__subchapter__chapter", "updated_at"]
    search_fields = ["student__email", "content__title", "content__slug"]
    list_select_related = ["student", "content", "content__subchapter"]
    date_hierarchy = "updated_at"

    @admin.display(description="Lesson", ordering="content__subchapter__title")
    def lesson(self, progress: VideoProgress) -> str:
        return progress.content.subchapter.title


@admin.register(SavedQuestion)
class SavedQuestionAdmin(ReadOnlyAdmin):
    list_display = ["student", "question", "saved_at"]
    search_fields = ["student__email", "question__key"]
    list_select_related = ["student", "question"]


class SubtitleForm(forms.ModelForm):
    class Meta:
        model = Subtitle
        fields = ["language", "file"]
        widgets = {"file": forms.ClearableFileInput(attrs={"accept": ".vtt,text/vtt"})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk:
            self.fields["language"].initial = default_subtitle_language()

    def has_changed(self) -> bool:
        if not self.instance.pk and self.add_prefix("file") not in self.files:
            chosen = self.data.get(self.add_prefix("language"))
            return bool(chosen) and chosen != default_subtitle_language()
        return super().has_changed()

    def clean_file(self):
        upload = self.cleaned_data.get("file")
        if isinstance(upload, UploadedFile):
            try:
                services.check_subtitle_upload(upload)
            except services.SubtitleUploadError as error:
                raise forms.ValidationError(str(error)) from error
        return upload


class SubtitleInlineFormSet(forms.BaseInlineFormSet):
    def clean(self):
        super().clean()
        if self.instance.kind in ("", MediaKind.VIDEO):
            return
        if any(form.cleaned_data and not form.cleaned_data.get("DELETE") for form in self.forms):
            raise forms.ValidationError(
                "Only a video can have subtitles. Set Kind to Video, or clear the subtitle tracks below."
            )


class SubtitleInline(admin.TabularInline):
    model = Subtitle
    form = SubtitleForm
    formset = SubtitleInlineFormSet
    extra = 1
    fields = ["language", "file", "current_file"]
    readonly_fields = ["current_file"]

    @admin.display(description="Uploaded file")
    def current_file(self, track: Subtitle) -> SafeString:
        if track.pk is None or not track.file:
            return placeholder()
        return format_html('<a class="adm-btn" href="{}" target="_blank">Open the .vtt</a>', track.file.url)


@admin.register(MediaAsset)
class MediaAssetAdmin(PermissionRequiredAdminMixin, admin.ModelAdmin):
    """The media library: files staff have uploaded, used by reference from content.

    Uploads go through here rather than straight from the browser to the bucket, so the bucket's
    credentials stay on the server and nothing arrives in it that staff did not put there.
    """

    inlines = [SubtitleInline]
    list_display = ["title", "kind", "readable_size", "subtitle_languages", "created_at"]
    list_filter = ["kind", "created_at"]
    search_fields = ["title", "file"]
    readonly_fields = ["size_bytes", "content_type", "uploaded_by", "created_at", "download_link", "content_reference"]
    fieldsets = (
        (None, {"fields": ("title", "kind", "file")}),
        (
            "About the file",
            {
                "fields": ("size_bytes", "content_type", "uploaded_by", "created_at", "download_link"),
                "description": "Taken from the upload itself. The link is signed and expires.",
            },
        ),
        (
            "Using it in content",
            {
                "fields": ("content_reference",),
                "description": (
                    "Pictures only. Paste this into the editor's Insert image → via URL "
                    "to show this picture in a lesson or an e-book section. It is a reference to "
                    "this library entry rather than a link to the file."
                ),
            },
        ),
    )

    @admin.display(description="Reference for the editor")
    def content_reference(self, asset: MediaAsset) -> SafeString:
        if asset.pk is None or asset.kind != MediaKind.IMAGE:
            return placeholder()
        return format_html("<code>{}</code>", html.reference_for(asset.pk))

    def get_queryset(self, request: HttpRequest):
        return super().get_queryset(request).prefetch_related("subtitles")

    def get_inline_instances(self, request: HttpRequest, obj=None):
        if obj is not None and obj.kind != MediaKind.VIDEO:
            return []
        return super().get_inline_instances(request, obj)

    @admin.display(description="Subtitles")
    def subtitle_languages(self, asset: MediaAsset) -> SafeString:
        languages = [track.language for track in asset.subtitles.all()]
        return format_html("{}", ", ".join(languages)) if languages else placeholder()

    @admin.display(description="Size", ordering="size_bytes")
    def readable_size(self, asset: MediaAsset) -> str:
        size = float(asset.size_bytes)
        for unit in ("bytes", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GB"

    @admin.display(description="File")
    def download_link(self, asset: MediaAsset) -> SafeString:
        if not asset.file:
            return placeholder()
        return format_html('<a class="adm-btn" href="{}" target="_blank">Open the file</a>', asset.file.url)

    def save_model(self, request: HttpRequest, obj: MediaAsset, form, change: bool) -> None:
        # Recorded here because the request knows who is uploading and the model does not.
        if not change:
            obj.uploaded_by = cast(User, request.user)
        super().save_model(request, obj, form, change)


class HazardWindowInline(admin.TabularInline):
    """When each hazard develops, edited beside the clip it belongs to.

    The timings are the clip: get one wrong and students are marked down for spotting a hazard
    correctly, which is why the preview plays the video with these marked on it.
    """

    model = HazardWindow
    extra = 1
    fields = ["order", "label", "starts_at", "ends_at", "bands"]
    readonly_fields = ["bands"]
    ordering = ["starts_at"]

    @admin.display(description="Scores 5 → 1 across")
    def bands(self, window: HazardWindow) -> SafeString:
        """Spells out the five bands, so staff can see what each second is worth.

        One band to a chip rather than a single run of text joined by separators. Five bands is
        roughly ninety characters, which a table cell will not break mid-word, so the row grew
        wider than the page and the last bands were simply unreachable off the right edge.
        Chips wrap onto as many lines as the column is wide enough for.
        """
        if window.pk is None or window.ends_at is None or window.starts_at is None:
            return placeholder()

        start, end = float(window.starts_at), float(window.ends_at)
        step = (end - start) / HazardWindow.BANDS
        edges = [start + step * band for band in range(HazardWindow.BANDS + 1)]
        chips = format_html_join(
            "",
            '<span class="adm-band"><span class="adm-band-time">{}–{}s</span>'
            '<span class="adm-band-score">{}</span></span>',
            (
                (f"{edges[band]:.1f}", f"{edges[band + 1]:.1f}", HazardWindow.BANDS - band)
                for band in range(HazardWindow.BANDS)
            ),
        )
        return format_html('<span class="adm-bands">{}</span>', chips)


@admin.register(HazardClip)
class HazardClipAdmin(PreviewMixin, PublishableAdmin):
    """Hazard perception clips: a video from the library, plus when its hazards develop."""

    list_display = ["still", "title", "hazard_count", "top_score", "max_clicks", "order", "state"]
    list_display_links = ["title"]
    list_editable = ["order"]
    list_filter = ["status"]
    search_fields = ["title", "slug", "description"]
    prepopulated_fields = {"slug": ("title",)}
    autocomplete_fields = ["media"]
    inlines = [HazardWindowInline]
    fieldsets = (
        (
            None,
            {
                "fields": ("title", "slug", "description", "media", "thumbnail", "thumbnail_preview", "order"),
                "description": (
                    "The video comes from the media library. The thumbnail is uploaded here and is "
                    "optional — tick 'Clear' beside it to remove one."
                ),
            },
        ),
        (
            "Scoring",
            {
                "fields": ("max_clicks", "duration_seconds"),
                "description": (
                    "Each hazard below is worth 5 down to 1, depending on how early in its window "
                    "the student clicks. Clicking more often than the limit scores nothing at all, "
                    "as does clicking in a regular pattern, in rapid bursts, or evenly across the "
                    "whole clip. The length is only needed for that last check."
                ),
            },
        ),
        (
            "Publishing",
            {"fields": ("status", "publish_from", "published_at", "published_by", "readiness", "preview_link")},
        ),
    )
    readonly_fields = ["status", "published_at", "published_by", "readiness", "preview_link", "thumbnail_preview"]

    @admin.display(description="Hazards")
    def hazard_count(self, clip: HazardClip) -> int:
        return clip.windows.count()

    @admin.display(description="Top score")
    def top_score(self, clip: HazardClip) -> int:
        return clip.top_score

    @admin.display(description="")
    def still(self, clip: HazardClip) -> SafeString:
        """The thumbnail in the list, so staff can see at a glance which clips have one."""
        return _thumbnail(clip.thumbnail, width=64)

    @admin.display(description="Shown before it plays")
    def thumbnail_preview(self, clip: HazardClip) -> SafeString:
        return _thumbnail(clip.thumbnail, width=320)


@admin.register(HazardAttempt)
class HazardAttemptAdmin(ReadOnlyAdmin):
    list_display = ["student", "clip", "result", "voided", "void_reason", "created_at"]
    list_filter = ["voided", "void_reason", "clip", "created_at"]
    search_fields = ["student__email", "clip__title"]
    list_select_related = ["student", "clip"]
    date_hierarchy = "created_at"

    @admin.display(description="Score")
    def result(self, attempt: HazardAttempt) -> str:
        return f"{attempt.score}/{attempt.top_score}"


class EbookSectionInline(ChildInline):
    model = EbookSection
    fields = ["title", "order", "state"]
    readonly_fields = ["state"]
    ordering = ["order"]
    verbose_name_plural = "Sections"


@admin.register(Ebook)
class EbookAdmin(PreviewMixin, PublishableAdmin):
    list_display = ["cover_thumbnail", "title", "slug", "file", "section_count", "order", "state"]
    # The title, not the picture, is what staff click to open one.
    list_display_links = ["title"]
    list_editable = ["order"]
    list_filter = ["status", "created_at"]
    search_fields = ["title", "slug", "description"]
    # Filled in from the title as staff type, and editable afterwards: the slug is in links, so a
    # published e-book's slug is a decision rather than something to regenerate.
    prepopulated_fields = {"slug": ("title",)}
    autocomplete_fields = ["file"]
    list_select_related = ["file"]
    date_hierarchy = "created_at"
    inlines = [EbookSectionInline]
    fieldsets = (
        (
            None,
            {
                "fields": ("title", "slug", "description", "order"),
                "description": "The slug is how the API addresses this e-book. Changing it breaks saved links.",
            },
        ),
        (
            "The book",
            {
                "fields": ("cover", "cover_thumbnail", "file"),
                "description": (
                    "The cover is uploaded here and is optional — tick 'Clear' beside it to remove "
                    "one. The file is a document from the media library, offered for download "
                    "beside the sections, and is optional too."
                ),
            },
        ),
        (
            "Publishing",
            {
                "fields": ("status", "publish_from", "published_at", "published_by", "readiness", "preview_link"),
                "description": "Students see this, and everything in it, only once it is published.",
            },
        ),
    )
    readonly_fields = [
        "status",
        "published_at",
        "published_by",
        "readiness",
        "preview_link",
        "cover_thumbnail",
        "created_at",
        "updated_at",
    ]

    @admin.display(description="")
    def cover_thumbnail(self, ebook: Ebook) -> SafeString:
        return _thumbnail(ebook.cover, width=90)

    @admin.display(description="Sections")
    def section_count(self, ebook: Ebook) -> str:
        published = ebook.sections.filter(status=PublishStatus.PUBLISHED).count()
        return f"{published} published / {ebook.sections.count()} total"


class EbookSectionForm(forms.ModelForm):
    body_html = forms.CharField(
        label="Content",
        required=False,
        widget=CKEditor5Widget(config_name="default"),
    )

    class Meta:
        model = EbookSection
        # Listed rather than "__all__", so a field added later is not exposed by accident.
        # Status is not here: publishing goes through the service, not the form.
        fields = ["ebook", "title", "slug", "summary", "body_html", "media", "order", "publish_from"]


@admin.register(EbookSection)
class EbookSectionAdmin(PreviewMixin, PublishableAdmin):
    form = EbookSectionForm
    list_display = ["title", "ebook", "order", "has_media", "sign_count", "state"]
    list_editable = ["order"]
    list_filter = ["status", "ebook"]
    search_fields = ["title", "slug", "summary", "ebook__title"]
    prepopulated_fields = {"slug": ("title",)}
    list_select_related = ["ebook", "media"]
    autocomplete_fields = ["ebook", "media"]
    ordering = ["ebook", "order"]
    fieldsets = (
        (None, {"fields": ("ebook", "title", "slug", "summary", "order")}),
        (
            "Content",
            {
                "fields": ("body_html", "media"),
                "description": format_html(
                    "The text is shown first, then the file if there is one. Pictures and GIFs go "
                    "in the text itself, where they stay in the place you put them: use "
                    "Insert image to upload one, or paste a picture's reference from the "
                    "Media assets library into the same button's via URL box. JPG, PNG, "
                    "WebP and GIF. To draw a strip of road signs in the middle of the text, write "
                    "<code>{}</code> on a line of its own — the caption after the bar is optional, "
                    "and the codes are the ones in the Signs library.",
                    "[signs: give-way, stop | Common warning signs]",
                ),
            },
        ),
        (
            "Publishing",
            {"fields": ("status", "publish_from", "published_at", "published_by", "readiness", "preview_link")},
        ),
    )
    readonly_fields = ["status", "published_at", "published_by", "readiness", "preview_link"]

    @admin.display(description="File", boolean=True)
    def has_media(self, section: EbookSection) -> bool:
        return section.media_id is not None

    @admin.display(description="Signs")
    def sign_count(self, section: EbookSection) -> int:
        """How many signs the text draws, so staff can see at a glance which sections show them."""
        return len(html.sign_codes_in(section.body_html))


@admin.register(EbookSectionProgress)
class EbookSectionProgressAdmin(ReadOnlyAdmin):
    list_display = ["student", "section", "book", "read_at"]
    list_filter = ["section__ebook", "read_at"]
    search_fields = ["student__email", "section__title"]
    list_select_related = ["student", "section", "section__ebook"]
    date_hierarchy = "read_at"

    @admin.display(description="Book", ordering="section__ebook__title")
    def book(self, progress: EbookSectionProgress) -> str:
        return progress.section.ebook.title
