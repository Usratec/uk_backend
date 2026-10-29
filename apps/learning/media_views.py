import logging
from typing import cast

from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.entitlements import services as entitlements
from apps.integrations import s3_client
from apps.learning import media_processing, media_services
from apps.learning.media_serializers import (
    MediaStatusResponseSerializer,
    PlaybackResponseSerializer,
    VideoUploadCompleteSerializer,
    VideoUploadPartsResponseSerializer,
    VideoUploadPartsSerializer,
    VideoUploadStartedResponseSerializer,
    VideoUploadStartSerializer,
)
from apps.learning.models import MediaAsset, MediaKind
from apps.learning.permissions import CanManageMedia
from apps.staff.authz import PermissionDeniedError

logger = logging.getLogger(__name__)

NOT_FOUND = "Not found."
PLAN_NEEDED = "An active plan is needed for this"
STORAGE_UNAVAILABLE = "The storage service is unavailable. Try again shortly."
NOT_READY = "This video is not ready to play yet."


def _error(message: str, code: int) -> Response:
    return Response({"detail": message}, status=code)


def _upload_failure(error: Exception) -> Response:
    if isinstance(error, PermissionDeniedError):
        return _error("You may not manage the media library.", status.HTTP_403_FORBIDDEN)
    if isinstance(error, media_services.UploadExpiredError):
        return _error(str(error), status.HTTP_410_GONE)
    if isinstance(error, media_services.UploadStateError):
        return _error(str(error), status.HTTP_409_CONFLICT)
    if isinstance(error, media_services.UploadVerificationError):
        return _error(str(error), status.HTTP_409_CONFLICT)
    if isinstance(error, media_services.UnsupportedVideoError | media_services.UploadTooLargeError):
        return _error(str(error), status.HTTP_400_BAD_REQUEST)
    if isinstance(error, media_services.StorageNotConfiguredError):
        logger.error("A direct upload was asked for, but no uploads bucket is configured")
        return _error(STORAGE_UNAVAILABLE, status.HTTP_503_SERVICE_UNAVAILABLE)
    if isinstance(error, s3_client.ObjectStorageError):
        logger.warning("A direct upload step could not reach the bucket")
        return _error(STORAGE_UNAVAILABLE, status.HTTP_503_SERVICE_UNAVAILABLE)
    return _error(str(error), status.HTTP_409_CONFLICT)


def _status_body(asset: MediaAsset) -> dict:
    summary = media_processing.playback_summary(asset) or {}
    return {
        "mediaId": str(asset.upload_token),
        "title": asset.title,
        "status": asset.processing_status,
        "playbackType": summary.get("playbackType", ""),
        "sizeBytes": asset.size_bytes,
        "durationSeconds": summary.get("durationSeconds"),
        "qualities": summary.get("qualities", []),
        "error": asset.processing_error,
        "attempts": asset.processing_attempts,
        "updatedAt": asset.updated_at,
    }


def _video_or_none(media_id) -> MediaAsset | None:
    return media_processing.media_for_playback(media_id)


class VideoUploadStartView(APIView):
    """Authorize one direct upload into the private bucket."""

    permission_classes = [IsAuthenticated, CanManageMedia]

    @extend_schema(
        operation_id="startVideoUpload",
        summary="Start a direct video upload",
        request=VideoUploadStartSerializer,
        responses={
            201: VideoUploadStartedResponseSerializer,
            400: None,
            403: None,
            503: None,
        },
    )
    def post(self, request: Request) -> Response:
        payload = VideoUploadStartSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        data = payload.validated_data

        try:
            started = media_services.start_video_upload(
                actor=cast(User, request.user),
                title=data.get("title", ""),
                filename=data["filename"],
                content_type=data.get("contentType", ""),
                size_bytes=data["sizeBytes"],
            )
        except (media_services.MediaUploadError, PermissionDeniedError, s3_client.ObjectStorageError) as error:
            return _upload_failure(error)

        return Response(
            {
                "mediaId": str(started.asset.upload_token),
                "mode": started.mode,
                "status": started.asset.processing_status,
                "uploadUrl": started.url,
                "partSizeBytes": started.part_size_bytes,
                "partCount": started.part_count,
                "urlExpiresIn": started.url_expires_in,
                "expiresAt": started.expires_at,
                "contentType": started.asset.content_type,
            },
            status=status.HTTP_201_CREATED,
        )


class VideoUploadPartsView(APIView):
    """Sign a batch of part URLs for an upload already under way."""

    permission_classes = [IsAuthenticated, CanManageMedia]

    @extend_schema(
        operation_id="signVideoUploadParts",
        summary="Sign the next parts of a multipart upload",
        request=VideoUploadPartsSerializer,
        responses={200: VideoUploadPartsResponseSerializer, 404: None, 409: None, 410: None},
    )
    def post(self, request: Request, media_id) -> Response:
        asset = _video_or_none(media_id)
        if asset is None:
            return _error(NOT_FOUND, status.HTTP_404_NOT_FOUND)

        payload = VideoUploadPartsSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            urls = media_services.sign_upload_parts(
                actor=cast(User, request.user), asset=asset, part_numbers=payload.validated_data["partNumbers"]
            )
        except (media_services.MediaUploadError, PermissionDeniedError, s3_client.ObjectStorageError) as error:
            return _upload_failure(error)

        return Response(
            {
                "urls": [{"partNumber": number, "url": url} for number, url in sorted(urls.items())],
                "urlExpiresIn": settings.VIDEO_UPLOAD_URL_EXPIRY_SECONDS,
            }
        )


class VideoUploadCompleteView(APIView):
    """Verify what arrived in the bucket and start processing it."""

    permission_classes = [IsAuthenticated, CanManageMedia]

    @extend_schema(
        operation_id="completeVideoUpload",
        summary="Finish a direct video upload",
        request=VideoUploadCompleteSerializer,
        responses={200: MediaStatusResponseSerializer, 404: None, 409: None, 410: None},
    )
    def post(self, request: Request, media_id) -> Response:
        asset = _video_or_none(media_id)
        if asset is None:
            return _error(NOT_FOUND, status.HTTP_404_NOT_FOUND)

        payload = VideoUploadCompleteSerializer(data=request.data or {})
        payload.is_valid(raise_exception=True)

        try:
            asset = media_services.complete_video_upload(
                actor=cast(User, request.user), asset=asset, parts=payload.validated_data.get("parts") or []
            )
        except (media_services.MediaUploadError, PermissionDeniedError, s3_client.ObjectStorageError) as error:
            return _upload_failure(error)

        return Response(_status_body(asset))


class VideoUploadCancelView(APIView):
    """Abandon an upload and clear away whatever reached the bucket."""

    permission_classes = [IsAuthenticated, CanManageMedia]

    @extend_schema(
        operation_id="cancelVideoUpload",
        summary="Cancel a direct video upload",
        request=None,
        responses={200: MediaStatusResponseSerializer, 404: None, 409: None},
    )
    def post(self, request: Request, media_id) -> Response:
        asset = _video_or_none(media_id)
        if asset is None:
            return _error(NOT_FOUND, status.HTTP_404_NOT_FOUND)

        try:
            asset = media_services.cancel_video_upload(actor=cast(User, request.user), asset=asset)
        except (media_services.MediaUploadError, PermissionDeniedError, s3_client.ObjectStorageError) as error:
            return _upload_failure(error)

        return Response(_status_body(asset))


class VideoStatusView(APIView):
    """Where one video is between being chosen and being ready to stream."""

    permission_classes = [IsAuthenticated, CanManageMedia]

    @extend_schema(
        operation_id="getVideoStatus",
        summary="Read a video's upload and processing status",
        responses={200: MediaStatusResponseSerializer, 404: None},
    )
    def get(self, request: Request, media_id) -> Response:
        asset = _video_or_none(media_id)
        if asset is None:
            return _error(NOT_FOUND, status.HTTP_404_NOT_FOUND)
        return Response(_status_body(asset))


class MediaPlaybackView(APIView):
    """Short-lived permission for this learner to play this video."""

    permission_classes = [IsAuthenticated]

    @extend_schema(
        operation_id="getMediaPlayback",
        summary="Authorize playback of a video",
        responses={200: PlaybackResponseSerializer, 402: None, 404: None, 409: None},
    )
    def get(self, request: Request, media_id) -> Response:
        student = cast(User, request.user)
        asset = media_processing.media_for_playback(media_id)
        if asset is None or asset.kind != MediaKind.VIDEO:
            return _error(NOT_FOUND, status.HTTP_404_NOT_FOUND)

        if not entitlements.has_access(student):
            return _error(PLAN_NEEDED, status.HTTP_402_PAYMENT_REQUIRED)

        try:
            playback = media_processing.playback_for(student=student, asset=asset)
        except media_processing.PlaybackDeniedError:
            return _error(PLAN_NEEDED, status.HTTP_402_PAYMENT_REQUIRED)
        except media_processing.PlaybackNotReadyError:
            return _error(NOT_READY, status.HTTP_409_CONFLICT)

        signed = playback.kind == media_processing.PLAYBACK_HLS
        response = Response(
            {
                "mediaId": playback.media_id,
                "playbackType": playback.kind,
                "url": playback.url,
                "expiresAt": playback.expires_at,
                "qualities": playback.qualities,
                "withCredentials": signed,
            }
        )

        for name, value in playback.cookies.items():
            response.set_cookie(
                name,
                value,
                domain=settings.CLOUDFRONT_COOKIE_DOMAIN or None,
                expires=playback.expires_at,
                path="/",
                secure=True,
                httponly=True,
                samesite="None",
            )
        return response
