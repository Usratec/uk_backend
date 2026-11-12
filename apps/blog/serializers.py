from django.urls import reverse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.blog.models import BlogCategory, BlogPost


class BlogCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = BlogCategory
        fields = ["name", "slug", "description"]


class CoverImageSerializer(serializers.Serializer):
    url = serializers.CharField()
    alt = serializers.CharField(help_text="What the cover shows, for readers who cannot see it.")
    width = serializers.IntegerField(allow_null=True)
    height = serializers.IntegerField(allow_null=True)


class StudyTipSerializer(serializers.Serializer):
    title = serializers.CharField()
    text = serializers.CharField()
    button_label = serializers.CharField(help_text="An empty string where the tip has no button.")
    button_link = serializers.CharField(help_text="An empty string where the tip has no button.")


class BlogPostSerializer(serializers.ModelSerializer):
    category = BlogCategorySerializer(read_only=True)
    author = serializers.CharField(
        source="byline",
        read_only=True,
        help_text="The byline to show, or an empty string where the post has no author.",
    )
    publish_date = serializers.DateTimeField(source="publish_at", read_only=True)
    reading_time = serializers.IntegerField(
        source="reading_minutes",
        read_only=True,
        help_text="Minutes the post takes to read, worked out from its content. 0 where it has none.",
    )
    cover_image = serializers.SerializerMethodField(
        help_text="The cover shown on the cards and at the top of the post, or null where none was uploaded.",
    )
    social_image_url = serializers.SerializerMethodField(
        help_text="A stable URL for the sharing image, or null where none was uploaded.",
    )

    class Meta:
        model = BlogPost
        fields = [
            "id",
            "title",
            "slug",
            "excerpt",
            "category",
            "author",
            "publish_date",
            "updated_at",
            "reading_time",
            "featured",
            "cover_image",
            "social_image_url",
            "meta_title",
            "meta_description",
            "canonical_url",
        ]

    @extend_schema_field(CoverImageSerializer(allow_null=True))
    def get_cover_image(self, post: BlogPost) -> dict | None:
        if not post.cover_image:
            return None
        return {
            "url": self._absolute(reverse("blog-post-cover-image", args=[post.slug])),
            "alt": post.cover_image_alt,
            "width": post.cover_image_width,
            "height": post.cover_image_height,
        }

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_social_image_url(self, post: BlogPost) -> str | None:
        if not post.social_image:
            return None
        return self._absolute(reverse("blog-post-social-image", args=[post.slug]))

    def _absolute(self, path: str) -> str:
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path


class BlogPostDetailSerializer(BlogPostSerializer):
    content = serializers.CharField(
        source="body_html",
        read_only=True,
        help_text="The post's rich text, already cleaned against an allowlist of tags.",
    )
    study_tip = serializers.SerializerMethodField(
        help_text="The Study tip box shown under the post, or null where the post has none.",
    )

    class Meta(BlogPostSerializer.Meta):
        fields = [*BlogPostSerializer.Meta.fields, "content", "study_tip", "created_at"]

    @extend_schema_field(StudyTipSerializer(allow_null=True))
    def get_study_tip(self, post: BlogPost) -> dict | None:
        if not (post.study_tip_title and post.study_tip_text):
            return None
        return {
            "title": post.study_tip_title,
            "text": post.study_tip_text,
            "button_label": post.study_tip_button_label,
            "button_link": post.study_tip_button_link,
        }


class BlogRedirectSerializer(serializers.Serializer):
    detail = serializers.CharField()
    slug = serializers.CharField(help_text="The post's current slug.")
    path = serializers.CharField(help_text="The path the reader should be sent to, e.g. /blog/new-slug/.")
