from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.notifications.models import Notification


class NotificationSerializer(serializers.ModelSerializer):
    link = serializers.SerializerMethodField(
        help_text="Where pressing the notification goes, as a frontend route, or null if nowhere."
    )

    class Meta:
        model = Notification
        fields = ["id", "kind", "title", "text", "link", "created_at", "is_read"]
        read_only_fields = fields

    @extend_schema_field(serializers.CharField(allow_null=True))
    def get_link(self, notification: Notification) -> str | None:
        return notification.link or None


class NotificationListSerializer(serializers.Serializer):
    unread_count = serializers.IntegerField(help_text="Unread notifications on the whole account, not just below.")
    results = NotificationSerializer(many=True)


class MarkAllReadSerializer(serializers.Serializer):
    updated = serializers.IntegerField(help_text="How many notifications this request marked as read.")


class PushPublicKeySerializer(serializers.Serializer):
    public_key = serializers.CharField()


class PushKeysSerializer(serializers.Serializer):
    p256dh = serializers.CharField(max_length=100)
    auth = serializers.CharField(max_length=32)

    def validate_p256dh(self, value: str) -> str:
        from apps.notifications.push_subscriptions import validate_p256dh

        return validate_p256dh(value)

    def validate_auth(self, value: str) -> str:
        from apps.notifications.push_subscriptions import validate_auth

        return validate_auth(value)


class PushEndpointSerializer(serializers.Serializer):
    endpoint = serializers.CharField(max_length=2048)

    def validate_endpoint(self, value: str) -> str:
        from apps.notifications.push_subscriptions import validate_endpoint

        return validate_endpoint(value)


class PushSubscriptionSerializer(PushEndpointSerializer):
    keys = PushKeysSerializer()


class PushLogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    push_endpoint = serializers.CharField(required=False, max_length=2048, allow_blank=True, allow_null=True)

    def validate_push_endpoint(self, value: str) -> str:
        from apps.notifications.push_subscriptions import validate_endpoint

        return validate_endpoint(value) if value else value
