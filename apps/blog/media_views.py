import logging
import mimetypes

from django.http import FileResponse, HttpResponse, HttpResponseRedirect
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET

from apps.blog import services
from apps.blog.models import BlogImage
from apps.core.uploads import delivery_headers

logger = logging.getLogger(__name__)

MISSING = "That picture is not here."


@require_GET
def image_file(request, key) -> HttpResponseBase:
    image = get_object_or_404(BlogImage, key=key)
    return _serve(image.file, image.content_type)


@require_GET
def social_image(request, slug: str) -> HttpResponseBase:
    post = services.live_post(slug=slug) or services.redirect_target(slug=slug)
    if post is None or not post.social_image:
        return HttpResponse(MISSING, status=404)
    return _serve(post.social_image, "")


def _serve(field, content_type: str) -> HttpResponseBase:
    if not field:
        return HttpResponse(MISSING, status=404)

    url = field.url
    if url.startswith(("http://", "https://")):
        return HttpResponseRedirect(url)

    headers = delivery_headers(content_type or _guessed_type(field.name), field.name)
    response = FileResponse(field.open("rb"), content_type=headers["Content-Type"])
    response["X-Content-Type-Options"] = headers["X-Content-Type-Options"]
    response["Content-Disposition"] = headers["Content-Disposition"]
    return response


def _guessed_type(name: str) -> str:
    return mimetypes.guess_type(name)[0] or ""
