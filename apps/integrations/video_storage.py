"""Route new pipeline files without moving the existing media library."""

from django.conf import settings
from django.core.files.storage import Storage, storages
from storages.backends.s3 import S3Storage  # type: ignore[import-untyped]


class VideoStorage(Storage):
    def __init__(self):
        self.legacy = storages["legacy"]
        self.video = S3Storage(
            bucket_name=settings.VIDEO_S3_BUCKET_NAME,
            region_name=settings.VIDEO_S3_REGION,
            signature_version="s3v4",
            default_acl=None,
            querystring_auth=True,
            querystring_expire=settings.S3_URL_EXPIRY_SECONDS,
            file_overwrite=False,
        )

    def _backend(self, name):
        prefixes = (settings.VIDEO_UPLOAD_PREFIX, settings.VIDEO_HLS_PREFIX)
        return self.video if any(name.startswith(f"{prefix.rstrip('/')}/") for prefix in prefixes) else self.legacy

    def _open(self, name, mode="rb"):
        return self._backend(name).open(name, mode)

    def _save(self, name, content):
        return self._backend(name).save(name, content)

    def exists(self, name):
        return self._backend(name).exists(name)

    def delete(self, name):
        return self._backend(name).delete(name)

    def size(self, name):
        return self._backend(name).size(name)

    def url(self, name):
        return self._backend(name).url(name)

    def get_modified_time(self, name):
        return self._backend(name).get_modified_time(name)
