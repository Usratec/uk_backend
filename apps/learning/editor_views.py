import logging

from django.http import FileResponse, HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.http.response import HttpResponseBase
from django.shortcuts import get_object_or_404
from django.views.decorators.http import require_GET, require_POST

from apps.learning import html, services
from apps.learning.models import MediaAsset, MediaKind
from apps.staff.access import granted_permissions

logger = logging.getLogger(__name__)

FORBIDDEN = "You may not use the media library."


def _may(request: HttpRequest, permission: str) -> bool:
    return request.user.is_active and request.user.is_staff and permission in granted_permissions(request.user)


@require_GET
def media_file(request: HttpRequest, pk: int) -> HttpResponseBase:
    if not _may(request, "learning.view_mediaasset"):
        return HttpResponse(FORBIDDEN, status=403)

    asset = get_object_or_404(MediaAsset, pk=pk, kind=MediaKind.IMAGE)
    if not asset.file:
        return HttpResponse("That picture has no file.", status=404)

    url = asset.file.url
    if url.startswith(("http://", "https://")):
        return HttpResponseRedirect(url)

    return FileResponse(asset.file.open("rb"), content_type=asset.content_type or None)


@require_POST
def media_upload(request: HttpRequest) -> JsonResponse:
    if not _may(request, "learning.add_mediaasset"):
        return JsonResponse({"error": {"message": FORBIDDEN}}, status=403)

    upload = request.FILES.get("upload")
    if upload is None:
        return JsonResponse({"error": {"message": "No file was sent."}}, status=400)

    try:
        asset = services.image_asset_from_upload(upload=upload, uploaded_by=request.user)
    except services.ImageUploadError as error:
        return JsonResponse({"error": {"message": str(error)}}, status=400)

    return JsonResponse({"url": html.reference_for(asset.pk)})
