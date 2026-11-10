import logging

from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_POST

from apps.blog import html, images, services
from apps.staff.access import granted_permissions

logger = logging.getLogger(__name__)

FORBIDDEN = "You may not add pictures to the blog."
NO_FILE = "No file was sent."


@require_POST
def image_upload(request: HttpRequest) -> JsonResponse:
    if not _may_upload(request):
        return JsonResponse({"error": {"message": FORBIDDEN}}, status=403)

    upload = request.FILES.get("upload")
    if upload is None:
        return JsonResponse({"error": {"message": NO_FILE}}, status=400)

    try:
        image = services.image_from_upload(upload=upload, uploaded_by=request.user)
    except images.ImageRejectedError as error:
        return JsonResponse({"error": {"message": str(error)}}, status=400)

    return JsonResponse({"url": html.reference_for(image.key)})


def _may_upload(request: HttpRequest) -> bool:
    user = request.user
    return user.is_active and user.is_staff and "blog.add_blogimage" in granted_permissions(user)
