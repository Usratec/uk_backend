"""Storage that will not keep a file until its bytes match a type the site can serve safely."""

from django.core.files.storage import FileSystemStorage, InMemoryStorage
from storages.backends.s3 import S3Storage  # type: ignore[import-untyped]  # ships no types

from apps.core import uploads


class CheckedUploadMixin:
    def _save(self, name, content):
        token = uploads.begin_stored_upload(content, name)
        try:
            return super()._save(name, content)  # type: ignore[misc]  # mixin; each subclass provides _save
        finally:
            uploads.finish_stored_upload(token)


class SafeFileSystemStorage(CheckedUploadMixin, FileSystemStorage):
    """Local disk, used when no bucket is configured."""


class SafeInMemoryStorage(CheckedUploadMixin, InMemoryStorage):
    """The test suite holds uploads in memory and still runs the same check."""


class SafeS3Storage(CheckedUploadMixin, S3Storage):
    """Private bucket. The object's type and disposition come from the bytes, not the browser."""

    def get_object_parameters(self, name):
        params = super().get_object_parameters(name)
        disposition = uploads.content_disposition()
        if disposition:
            # inline for pictures, video and subtitles, so the player can use them.
            # attachment for documents and SVG, so opening the link does not render a page.
            params["ContentDisposition"] = disposition
        return params
