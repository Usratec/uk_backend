from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from apps.notifications.models import DeviceToken, Notification


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


class DeviceTokenRemoveSerializer(serializers.Serializer):
    token = serializers.RegexField(r"^[!-~]+$", max_length=2048, trim_whitespace=False, write_only=True)


class DeviceTokenSerializer(DeviceTokenRemoveSerializer):
    platform = serializers.ChoiceField(choices=DeviceToken.Platform.choices)
