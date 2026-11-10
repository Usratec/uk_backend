from django.urls import path

from apps.blog import editor_views, media_views, views

urlpatterns = [
    path("blog/images/upload/", editor_views.image_upload, name="blog-image-upload"),
    path("blog/images/<uuid:key>/", media_views.image_file, name="blog-image-file"),
    path("blog/", views.BlogPostListView.as_view(), name="blog-post-list"),
    path("blog/<slug:slug>/", views.BlogPostDetailView.as_view(), name="blog-post"),
    path("blog/<slug:slug>/social-image/", media_views.social_image, name="blog-post-social-image"),
]
