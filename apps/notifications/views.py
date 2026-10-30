from typing import cast

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.accounts.serializers import DetailSerializer
from apps.notifications import push_subscriptions, services
from apps.notifications.serializers import (
    MarkAllReadSerializer,
    NotificationListSerializer,
    NotificationSerializer,
    PushEndpointSerializer,
    PushPublicKeySerializer,
    PushSubscriptionSerializer,
)

NOT_FOUND = "Notification not found."


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


class PushPublicKeyView(APIView):
    permission_classes = [AllowAny]

    @extend_schema(
        responses={200: PushPublicKeySerializer, 503: DetailSerializer}, summary="Get the Web Push public key"
    )
    def get(self, request: Request) -> Response:
        public_key = settings.WEB_PUSH_VAPID_PUBLIC_KEY
        if not public_key:
            return Response({"detail": "Web Push is not configured."}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"public_key": public_key})


class PushSubscriptionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        request=PushSubscriptionSerializer,
        responses={200: DetailSerializer, 409: DetailSerializer},
        summary="Save this browser's push subscription",
    )
    def post(self, request: Request) -> Response:
        data = PushSubscriptionSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        values = data.validated_data
        try:
            push_subscriptions.save_subscription(
                user=cast(User, request.user), endpoint=values["endpoint"], **values["keys"]
            )
        except push_subscriptions.SubscriptionConflictError:
            return Response({"detail": "Subscribe this browser again."}, status=status.HTTP_409_CONFLICT)
        return Response({"detail": "Push subscription saved."})

    @extend_schema(
        request=PushEndpointSerializer, responses={204: None}, summary="Remove this browser's push subscription"
    )
    def delete(self, request: Request) -> Response:
        data = PushEndpointSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        push_subscriptions.remove_subscription(user=cast(User, request.user), endpoint=data.validated_data["endpoint"])
        return Response(status=status.HTTP_204_NO_CONTENT)
