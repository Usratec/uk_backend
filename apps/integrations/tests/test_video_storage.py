from unittest.mock import Mock, patch

from django.core.files.storage import InMemoryStorage

from apps.integrations import s3_client
from apps.integrations.video_storage import VideoStorage


def test_dedicated_bucket_uses_task_role_instead_of_legacy_credentials(settings):
    settings.VIDEO_S3_BUCKET_NAME = "private-video-test"
    settings.VIDEO_S3_REGION = "eu-west-2"
    settings.S3_ENDPOINT_URL = "https://legacy.example.test"
    settings.S3_ACCESS_KEY_ID = "legacy-access"
    settings.S3_SECRET_ACCESS_KEY = "legacy-secret"  # noqa: S105
    settings.S3_CONFIGURED = False
    with patch("apps.integrations.s3_client.boto3.client") as client:
        s3_client.presigned_put_url(key="learning/video-source/a.mp4", content_type="video/mp4", expires_in=60)
    kwargs = client.call_args.kwargs
    assert kwargs["region_name"] == "eu-west-2"
    assert "endpoint_url" not in kwargs
    assert "aws_access_key_id" not in kwargs
    assert "aws_secret_access_key" not in kwargs
    signed = client.return_value.generate_presigned_url.call_args.kwargs
    assert signed["Params"]["Bucket"] == "private-video-test"


def test_new_video_paths_and_legacy_media_use_their_respective_storage(settings):
    settings.VIDEO_S3_BUCKET_NAME = "private-video-test"
    settings.VIDEO_S3_REGION = "eu-west-2"
    settings.STORAGES = {"legacy": {"BACKEND": "django.core.files.storage.InMemoryStorage"}}
    with patch("apps.integrations.video_storage.S3Storage") as backend:
        storage = VideoStorage()
    for path in ["learning/video-source/a.mp4", "learning/video-hls/a/master.m3u8"]:
        storage.url(path)
        backend.return_value.url.assert_called_with(path)
    assert isinstance(storage.legacy, InMemoryStorage)
    for path in ["learning/video/old.mp4", "learning/image/sign.png", "learning/video-source-other/a.mp4"]:
        assert storage.url(path) == f"/api/media/{path}"


def test_video_delete_and_open_never_touch_legacy_storage(settings):
    settings.STORAGES = {"legacy": {"BACKEND": "django.core.files.storage.InMemoryStorage"}}
    with patch("apps.integrations.video_storage.S3Storage"):
        storage = VideoStorage()
    storage.legacy = Mock()
    path = "learning/video-source/a.mp4"
    storage.delete(path)
    storage.open(path, "rb")
    storage.video.delete.assert_called_once_with(path)
    storage.video.open.assert_called_once_with(path, "rb")
    storage.legacy.delete.assert_not_called()
    storage.legacy.open.assert_not_called()


def test_reencoding_a_legacy_source_reads_the_existing_bucket(settings, tmp_path):
    settings.VIDEO_S3_BUCKET_NAME = "new-video"
    settings.VIDEO_S3_REGION = "eu-west-2"
    settings.S3_BUCKET_NAME = "existing-media"
    settings.S3_REGION = "eu-west-2"
    settings.S3_ENDPOINT_URL = ""
    settings.S3_ACCESS_KEY_ID = ""
    settings.S3_SECRET_ACCESS_KEY = ""
    destination = str(tmp_path / "source.mp4")
    with patch("apps.integrations.s3_client.boto3.client") as client:
        s3_client.download_to(key="learning/video/old.mp4", destination=destination)
    client.return_value.download_file.assert_called_once_with(
        Bucket="existing-media", Key="learning/video/old.mp4", Filename=destination
    )
