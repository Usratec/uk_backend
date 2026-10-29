from rest_framework import serializers

from apps.learning.media_services import MAX_PART_URLS_PER_REQUEST, S3_MAXIMUM_PARTS
from apps.learning.models import VideoProcessingStatus


class VideoUploadStartSerializer(serializers.Serializer):
    title = serializers.CharField(
        max_length=160, required=False, allow_blank=True, help_text="How staff will find this file again."
    )
    filename = serializers.CharField(
        max_length=255,
        help_text="The name of the file being uploaded. Only its extension is used, never its path.",
    )
    contentType = serializers.CharField(  # noqa: N815
        max_length=100,
        required=False,
        allow_blank=True,
        help_text="The browser's reported type. Checked against the extension and re-checked after upload.",
    )
    sizeBytes = serializers.IntegerField(  # noqa: N815
        min_value=1, help_text="How many bytes will be sent. Checked again against what actually arrives."
    )


class UploadPartSerializer(serializers.Serializer):
    partNumber = serializers.IntegerField(min_value=1, max_value=S3_MAXIMUM_PARTS)  # noqa: N815
    etag = serializers.CharField(max_length=128, help_text="The ETag the bucket returned for this part.")


class VideoUploadPartsSerializer(serializers.Serializer):
    partNumbers = serializers.ListField(  # noqa: N815
        child=serializers.IntegerField(min_value=1, max_value=S3_MAXIMUM_PARTS),
        min_length=1,
        max_length=MAX_PART_URLS_PER_REQUEST,
        help_text=f"Which parts to sign, at most {MAX_PART_URLS_PER_REQUEST} at a time.",
    )


class VideoUploadCompleteSerializer(serializers.Serializer):
    parts = UploadPartSerializer(
        many=True,
        required=False,
        help_text="Multipart uploads only. Checked against the part list the bucket itself holds.",
    )


class VideoUploadStartedResponseSerializer(serializers.Serializer):
    mediaId = serializers.UUIDField(help_text="How every later request refers to this file.")  # noqa: N815
    mode = serializers.ChoiceField(choices=[("single", "single"), ("multipart", "multipart")])
    status = serializers.ChoiceField(choices=VideoProcessingStatus.choices)
    uploadUrl = serializers.URLField(  # noqa: N815
        allow_blank=True, help_text="Single uploads only: PUT the file here with the given Content-Type."
    )
    partSizeBytes = serializers.IntegerField(help_text="Multipart only: how big every part but the last must be.")  # noqa: N815
    partCount = serializers.IntegerField(help_text="Multipart only: how many parts the file divides into.")  # noqa: N815
    urlExpiresIn = serializers.IntegerField(help_text="Seconds an upload URL stays valid.")  # noqa: N815
    expiresAt = serializers.DateTimeField(help_text="When the upload can no longer be finished.")  # noqa: N815
    contentType = serializers.CharField(help_text="Send exactly this Content-Type, or the bucket refuses the PUT.")  # noqa: N815


class SignedPartSerializer(serializers.Serializer):
    partNumber = serializers.IntegerField()  # noqa: N815
    url = serializers.URLField()


class VideoUploadPartsResponseSerializer(serializers.Serializer):
    urls = SignedPartSerializer(many=True)
    urlExpiresIn = serializers.IntegerField()  # noqa: N815


class MediaStatusResponseSerializer(serializers.Serializer):
    mediaId = serializers.UUIDField()  # noqa: N815
    title = serializers.CharField()
    status = serializers.ChoiceField(choices=VideoProcessingStatus.choices)
    playbackType = serializers.CharField(  # noqa: N815
        allow_blank=True, help_text="`hls` once processed and signed playback is configured, else `progressive`."
    )
    sizeBytes = serializers.IntegerField()  # noqa: N815
    durationSeconds = serializers.FloatField(allow_null=True)  # noqa: N815
    qualities = serializers.ListField(child=serializers.CharField(), help_text="The renditions that were produced.")
    error = serializers.CharField(allow_blank=True, help_text="A short summary when processing failed.")
    attempts = serializers.IntegerField(help_text="How many times processing has been tried.")
    updatedAt = serializers.DateTimeField()  # noqa: N815


class PlaybackResponseSerializer(serializers.Serializer):
    mediaId = serializers.UUIDField()  # noqa: N815
    playbackType = serializers.ChoiceField(  # noqa: N815
        choices=[("hls", "hls"), ("progressive", "progressive")],
        help_text="`hls` means an adaptive master playlist; `progressive` means a single file.",
    )
    url = serializers.URLField(help_text="Where to play from. Short-lived — fetch it again rather than storing it.")
    expiresAt = serializers.DateTimeField(help_text="When this authorization stops working.")  # noqa: N815
    qualities = serializers.ListField(
        child=serializers.CharField(), help_text="The quality levels in the manifest, for a player's menu."
    )
    withCredentials = serializers.BooleanField(  # noqa: N815
        help_text=(
            "True for `hls`: the playback cookies were set on this response, so every manifest and "
            "segment request must be made with credentials."
        )
    )
