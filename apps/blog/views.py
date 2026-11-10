from django.urls import reverse
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.serializers import DetailSerializer
from apps.blog import services
from apps.blog.pagination import BlogPagination
from apps.blog.serializers import BlogPostDetailSerializer, BlogPostSerializer, BlogRedirectSerializer

NOT_FOUND = "Not found."
MOVED = "This post has moved to a new address."

CATEGORY_PARAMETER = OpenApiParameter(
    "category",
    str,
    OpenApiParameter.QUERY,
    required=False,
    description="A category slug. Only posts in that category are returned.",
)


@extend_schema(
    summary="List the published blog posts",
    description=(
        "Newest first, paginated. Drafts, posts that have been unpublished and posts scheduled for "
        "a time that has not arrived are never returned."
    ),
    parameters=[CATEGORY_PARAMETER],
)
class BlogPostListView(ListAPIView):
    permission_classes = [AllowAny]
    serializer_class = BlogPostSerializer
    pagination_class = BlogPagination

    def get_queryset(self):
        return services.live_posts(category=self.request.query_params.get("category", ""))


class BlogPostDetailView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        summary="Read one published blog post",
        description=(
            "Returns a published post by slug, including content and SEO metadata. "
            "Outdated slugs return `301` with the new URL and current post details. "
            "Unpublished or unavailable posts return `404`."
        ),
        parameters=[OpenApiParameter("slug", str, OpenApiParameter.PATH, description="The post's slug.")],
        responses={
            200: BlogPostDetailSerializer,
            301: BlogRedirectSerializer,
            404: DetailSerializer,
        },
    )
    def get(self, request: Request, slug: str) -> Response:
        post = services.live_post(slug=slug)
        if post is not None:
            return Response(BlogPostDetailSerializer(post, context={"request": request}).data)

        moved = services.redirect_target(slug=slug)
        if moved is not None:
            return Response(
                {"detail": MOVED, "slug": moved.slug, "path": services.path_of(moved)},
                status=status.HTTP_301_MOVED_PERMANENTLY,
                headers={"Location": reverse("blog-post", args=[moved.slug])},
            )

        return Response({"detail": NOT_FOUND}, status=status.HTTP_404_NOT_FOUND)
