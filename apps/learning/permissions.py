from rest_framework.permissions import BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from apps.staff.access import granted_permissions

NOT_A_CONTENT_MANAGER = "You may not manage the media library."


class CanManageMedia(BasePermission):
    """Lets through only staff who were granted the media library's add permission."""

    message = NOT_A_CONTENT_MANAGER
    required_permission = "learning.add_mediaasset"

    def has_permission(self, request: Request, view: APIView) -> bool:
        user = request.user
        if not user.is_authenticated or not user.is_active or not user.is_staff:
            return False
        return self.required_permission in granted_permissions(user)
