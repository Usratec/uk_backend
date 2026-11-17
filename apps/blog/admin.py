from typing import cast

from django import forms
from django.contrib import admin, messages
from django.db.models import Count, Q, QuerySet
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import URLPattern, path, reverse
from django.utils.html import format_html, format_html_join
from django.utils.safestring import SafeString, mark_safe

from apps.accounts.models import User
from apps.blog import services
from apps.blog.models import BlogCategory, BlogImage, BlogPost, BlogPostRedirect, BlogStatus
from apps.blog.roles import BLOG_MANAGER
from apps.blog.widgets import BlogCKEditor5Widget
from apps.staff.access import granted_permissions
from apps.staff.admin import PermissionRequiredAdminMixin, badge, placeholder
from apps.staff.authz import PermissionDeniedError

STATUS_TONES: dict[str, str] = {
    BlogStatus.PUBLISHED: "success",
    BlogStatus.SCHEDULED: "neutral",
    BlogStatus.DRAFT: "neutral",
}

NO_PUBLISH_PERMISSION = "You may not publish or unpublish blog posts."


class BlogPostForm(forms.ModelForm):
    body_html = forms.CharField(
        label="Content",
        required=False,
        widget=BlogCKEditor5Widget(config_name="default"),
    )

    class Meta:
        model = BlogPost
        fields = [
            "title",
            "slug",
            "category",
            "featured",
            "author",
            "author_name",
            "excerpt",
            "body_html",
            "cover_image",
            "cover_image_alt",
            "study_tip_title",
            "study_tip_text",
            "study_tip_button_label",
            "study_tip_button_link",
            "publish_at",
            "meta_title",
            "meta_description",
            "canonical_url",
            "social_image",
        ]


@admin.register(BlogCategory)
class BlogCategoryAdmin(PermissionRequiredAdminMixin, admin.ModelAdmin):
    list_display = ["name", "slug", "post_count", "description"]
    search_fields = ["name", "slug", "description"]
    prepopulated_fields = {"slug": ("name",)}
    fields = ["name", "slug", "description"]

    def get_queryset(self, request: HttpRequest) -> QuerySet:
        return super().get_queryset(request).annotate(_posts=Count("posts"))

    @admin.display(description="Posts", ordering="_posts")
    def post_count(self, category: BlogCategory) -> int:
        return getattr(category, "_posts", 0)


@admin.register(BlogImage)
class BlogImageAdmin(PermissionRequiredAdminMixin, admin.ModelAdmin):
    list_display = ["thumbnail", "title", "created_at", "uploaded_by"]
    list_display_links = ["title"]
    search_fields = ["title"]
    list_select_related = ["uploaded_by"]
    readonly_fields = ["preview", "reference", "created_at"]

    def get_fields(self, request: HttpRequest, obj=None) -> list:
        if obj is None:
            return ["title", "file"]
        return ["title", "file", "preview", "reference", "created_at"]

    @admin.display(description="Picture")
    def thumbnail(self, image: BlogImage) -> SafeString:
        if not image.file:
            return placeholder()
        return format_html(
            '<img src="{}" style="height:48px;width:72px;object-fit:cover;border-radius:4px" alt="">',
            reverse("blog-image-file", args=[image.key]),
        )

    @admin.display(description="Picture")
    def preview(self, image: BlogImage) -> SafeString:
        if not image.pk or not image.file:
            return placeholder()
        return format_html(
            '<img src="{}" style="max-width:100%;max-height:420px" alt="">',
            reverse("blog-image-file", args=[image.key]),
        )

    @admin.display(description="Address to paste into a post")
    def reference(self, image: BlogImage) -> SafeString:
        if not image.pk:
            return placeholder()
        return format_html(
            '{}<p class="help">The editor writes this address itself when you insert a picture '
            "while writing. Copy it only if you are putting the same picture into another post "
            "by hand.</p>",
            image.reference,
        )

    def save_model(self, request: HttpRequest, obj: BlogImage, form, change) -> None:
        if obj.uploaded_by_id is None:
            obj.uploaded_by = cast(User, request.user)
        super().save_model(request, obj, form, change)


@admin.register(BlogPostRedirect)
class BlogPostRedirectAdmin(PermissionRequiredAdminMixin, admin.ModelAdmin):
    list_display = ["old_slug", "post", "current_slug", "created_at"]
    search_fields = ["old_slug", "post__slug", "post__title"]
    list_select_related = ["post"]
    readonly_fields = ["old_slug", "post", "created_at"]
    actions = ["delete_selected"]

    @admin.display(description="Now points at")
    def current_slug(self, redirect: BlogPostRedirect) -> str:
        return redirect.post.slug

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj=None) -> bool:
        return False


@admin.register(BlogPost)
class BlogPostAdmin(PermissionRequiredAdminMixin, admin.ModelAdmin):
    form = BlogPostForm
    preview_template = "admin/blog/preview.html"
    list_display = ["title", "category", "byline", "publish_at", "state", "featured"]
    list_filter = ["status", "category", "featured"]
    search_fields = ["title", "slug", "meta_title", "meta_description"]
    prepopulated_fields = {"slug": ("title",)}
    list_select_related = ["category", "author"]
    autocomplete_fields = ["category"]
    date_hierarchy = "publish_at"
    actions = ["publish_selected", "schedule_selected", "unpublish_selected"]
    readonly_fields = [
        "status",
        "published_by",
        "reading_time",
        "readiness",
        "preview_link",
        "former_addresses",
    ]
    fieldsets = (
        (None, {"fields": ("title", "slug", "category", "featured", "author", "author_name")}),
        (
            "Content",
            {
                "fields": ("excerpt", "body_html", "reading_time"),
                "description": "The summary heads the cards on the blog; the content is the article itself.",
            },
        ),
        (
            "Cover picture",
            {
                "fields": ("cover_image", "cover_image_alt"),
                "description": "Shown on the cards and at the top of the post, unlike the sharing image below.",
            },
        ),
        (
            "Study tip",
            {
                "fields": (
                    "study_tip_title",
                    "study_tip_text",
                    "study_tip_button_label",
                    "study_tip_button_link",
                ),
                "classes": ["collapse"],
                "description": (
                    "Optional. Fill the title and the tip to show a Study tip box under the post. "
                    "A button needs both a label and a link, or neither."
                ),
            },
        ),
        (
            "Search and sharing",
            {
                "fields": ("meta_title", "meta_description", "canonical_url", "social_image"),
                "description": "What search engines and social networks show. All optional.",
            },
        ),
        (
            "Publishing",
            {
                "fields": ("status", "publish_at", "published_by", "readiness", "preview_link"),
                "description": (
                    "Saving never publishes. Choose a publish date, then use an action on the list "
                    "to publish now or to schedule it for that date."
                ),
            },
        ),
        (
            "Addresses",
            {
                "fields": ("former_addresses",),
                "classes": ["collapse"],
                "description": (
                    "Changing the address above leaves the old one behind as a redirect, so links "
                    "and search results that already point at it keep working. Redirects are "
                    "written by renaming rather than by hand; delete one under Blog redirects to "
                    "free an address up again."
                ),
            },
        ),
    )

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "author":
            kwargs["queryset"] = (
                db_field.related_model.objects.filter(is_staff=True)
                .filter(Q(groups__name=BLOG_MANAGER.name) | Q(blog_posts__isnull=False))
                .order_by("first_name", "last_name")
                .distinct()
            )
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    @admin.display(description="Author")
    def byline(self, post: BlogPost) -> str:
        return post.byline or "—"

    @admin.display(description="Status", ordering="status")
    def state(self, post: BlogPost) -> SafeString:
        return badge(post.get_status_display(), STATUS_TONES.get(post.status, "neutral"))

    @admin.display(description="Reading time")
    def reading_time(self, post: BlogPost) -> str:
        if post.pk is None or not post.reading_minutes:
            return "—"
        return f"{post.reading_minutes} min"

    @admin.display(description="Ready to publish")
    def readiness(self, post: BlogPost) -> SafeString:
        if post.pk is None:
            return placeholder()

        problems = services.problems_with(post)
        if problems:
            listed = format_html_join(mark_safe("<br>"), "&bull; {}", ((problem,) for problem in problems))
            return format_html("Not yet:<br>{}", listed)

        warnings = services.warnings_about(post)
        if warnings:
            listed = format_html_join(mark_safe("<br>"), "&bull; {}", ((warning,) for warning in warnings))
            return format_html("{}<br>{}", badge("Ready", "success"), listed)

        return badge("Ready", "success")

    @admin.display(description="Preview")
    def preview_link(self, post: BlogPost) -> SafeString:
        if post.pk is None:
            return placeholder()
        url = reverse("admin:blog_blogpost_preview", args=[post.pk])
        return format_html('<a class="adm-btn" href="{}" target="_blank">See it as a reader</a>', url)

    @admin.display(description="Old addresses that still work")
    def former_addresses(self, post: BlogPost) -> SafeString:
        if post.pk is None:
            return placeholder()

        slugs = list(post.redirects.values_list("old_slug", flat=True))
        if not slugs:
            return mark_safe("<span class='adm-muted'>None. The address has never changed.</span>")  # noqa: S308 - fixed markup
        return format_html_join(mark_safe("<br>"), "{}", ((slug,) for slug in slugs))

    def get_urls(self) -> list[URLPattern]:
        preview = path(
            "<int:object_id>/preview/",
            self.admin_site.admin_view(self.preview_view),
            name="blog_blogpost_preview",
        )
        return [preview, *super().get_urls()]

    def preview_view(self, request: HttpRequest, object_id: int) -> HttpResponse:
        if not self.has_view_permission(request):
            return HttpResponse("You may not preview blog posts.", status=403)

        post = get_object_or_404(BlogPost, pk=object_id)
        return render(
            request,
            self.preview_template,
            {
                **self.admin_site.each_context(request),
                "title": f"Preview: {post}",
                "post": post,
                "problems": services.problems_with(post),
                "warnings": services.warnings_about(post),
                "opts": self.model._meta,
            },
        )

    def save_model(self, request: HttpRequest, obj: BlogPost, form, change) -> None:
        previous_slug = ""
        if change:
            previous_slug = BlogPost.objects.filter(pk=obj.pk).values_list("slug", flat=True).first() or ""
        services.save_post(actor=request.user, post=obj, previous_slug=previous_slug)

    def has_publish_permission(self, request: HttpRequest) -> bool:
        return services.PUBLISH_PERMISSION in granted_permissions(request.user)

    def get_actions(self, request: HttpRequest):
        actions = super().get_actions(request)
        if not self.has_publish_permission(request):
            for name in ("publish_selected", "schedule_selected", "unpublish_selected"):
                actions.pop(name, None)
        return actions

    @admin.action(description="Publish the selected posts now")
    def publish_selected(self, request: HttpRequest, queryset: QuerySet) -> None:
        self._run(request, queryset, services.publish, "Published")

    @admin.action(description="Schedule the selected posts for their publish date")
    def schedule_selected(self, request: HttpRequest, queryset: QuerySet) -> None:
        self._run(request, queryset, services.schedule, "Scheduled")

    @admin.action(description="Take the selected posts off the site")
    def unpublish_selected(self, request: HttpRequest, queryset: QuerySet) -> None:
        self._run(request, queryset, services.unpublish, "Unpublished")

    def _run(self, request: HttpRequest, queryset: QuerySet, action, done: str) -> None:
        changed = 0
        for post in queryset:
            try:
                action(actor=request.user, post=post)
                changed += 1
            except (services.PostNotReadyError, services.ScheduleInThePastError) as error:
                self.message_user(request, f"{post}: {error}", level=messages.ERROR)
            except PermissionDeniedError:
                self.message_user(request, NO_PUBLISH_PERMISSION, level=messages.ERROR)
                return
        if changed:
            self.message_user(request, f"{done} {changed} post(s).", level=messages.SUCCESS)
