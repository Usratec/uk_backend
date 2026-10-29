from typing import cast

from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.serializers import DetailSerializer
from apps.accounts.throttling import AuthUserThrottle
from apps.notifications import services
from apps.notifications.serializers import (
    DeviceTokenRemoveSerializer,
    DeviceTokenSerializer,
    MarkAllReadSerializer,
    NotificationListSerializer,
    NotificationSerializer,
)

NOT_FOUND = "Notification not found."


class DeviceTokenView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_classes = [AuthUserThrottle]

    @extend_schema(
        request=DeviceTokenSerializer, responses={204: None}, summary="Save this device for push notifications"
    )
    def post(self, request: Request) -> Response:
        from apps.notifications.push import register_device

        data = DeviceTokenSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        register_device(user=cast(User, request.user), **data.validated_data)
        return Response(status=status.HTTP_204_NO_CONTENT)

    @extend_schema(request=DeviceTokenRemoveSerializer, responses={204: None}, summary="Stop push notifications here")
    def delete(self, request: Request) -> Response:
        from apps.notifications.push import remove_device_token

        data = DeviceTokenRemoveSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        remove_device_token(user=cast(User, request.user), **data.validated_data)
        return Response(status=status.HTTP_204_NO_CONTENT)


class NotificationListView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        responses={200: NotificationListSerializer},
        summary="List your notifications",
    )
    def get(self, request: Request) -> Response:
        user = cast(User, request.user)
        return Response(
            {
                "unread_count": services.unread_count(user),
                "results": NotificationSerializer(services.recent(user), many=True).data,
            }
        )


class NotificationReadView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=None,
        responses={200: DetailSerializer, 404: DetailSerializer},
        summary="Mark one notification as read",
    )
    def post(self, request: Request, pk: int) -> Response:
        user = cast(User, request.user)

        if not services.mark_read(user=user, notification_id=pk):
            return Response({"detail": NOT_FOUND}, status=status.HTTP_404_NOT_FOUND)

        return Response({"detail": "Notification marked as read."})


class NotificationReadAllView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=None,
        responses={200: MarkAllReadSerializer},
        summary="Mark all your notifications as read",
    )
    def post(self, request: Request) -> Response:
        user = cast(User, request.user)
        return Response({"updated": services.mark_all_read(user=user)})
