from django.urls import reverse
from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.blog.models import BlogCategory, BlogPost


class BlogCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = BlogCategory
        fields = ["name", "slug", "description"]


class BlogPostSerializer(serializers.ModelSerializer):
    category = BlogCategorySerializer(read_only=True)
    author = serializers.CharField(
        source="byline",
        read_only=True,
        help_text="The byline to show, or an empty string where the post has no author.",
    )
    publish_date = serializers.DateTimeField(source="publish_at", read_only=True)
    social_image_url = serializers.SerializerMethodField(
        help_text="A stable URL for the sharing image, or null where none was uploaded.",
    )

    class Meta:
        model = BlogPost
        fields = [
            "id",
            "title",
            "slug",
            "category",
            "author",
            "publish_date",
            "social_image_url",
            "meta_title",
            "meta_description",
            "canonical_url",
        ]

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_social_image_url(self, post: BlogPost) -> str | None:
        if not post.social_image:
            return None
        path = reverse("blog-post-social-image", args=[post.slug])
        request = self.context.get("request")
        return request.build_absolute_uri(path) if request else path


class BlogPostDetailSerializer(BlogPostSerializer):
    content = serializers.CharField(
        source="body_html",
        read_only=True,
        help_text="The post's rich text, already cleaned against an allowlist of tags.",
    )

    class Meta(BlogPostSerializer.Meta):
        fields = [*BlogPostSerializer.Meta.fields, "content", "created_at", "updated_at"]


class BlogRedirectSerializer(serializers.Serializer):
    detail = serializers.CharField()
    slug = serializers.CharField(help_text="The post's current slug.")
    path = serializers.CharField(help_text="The path the reader should be sent to, e.g. /blog/new-slug/.")
