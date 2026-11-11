from django.core.exceptions import ValidationError

from apps.core import images as core_images
from apps.core.uploads import RASTER_IMAGE_TYPES, require_upload

MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_MB = MAX_IMAGE_BYTES // (1024 * 1024)

ALLOWED_IMAGE_TYPES = RASTER_IMAGE_TYPES
ALLOWED_EXTENSIONS = ("jpg", "jpeg", "png", "webp", "gif")

TOO_LARGE = f"The picture must be under {MAX_IMAGE_MB} MB."
WRONG_FORMAT = f"Only {', '.join(name.upper() for name in ALLOWED_EXTENSIONS)} pictures can be used here."


class ImageRejectedError(Exception):
    """The file staff chose cannot be stored as a blog picture."""


def check_size(upload) -> None:
    if (getattr(upload, "size", 0) or 0) > MAX_IMAGE_BYTES:
        raise ImageRejectedError(TOO_LARGE)


def check_extension(upload) -> None:
    name = getattr(upload, "name", "") or ""
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in ALLOWED_EXTENSIONS:
        raise ImageRejectedError(WRONG_FORMAT)


def check_image(upload) -> str:
    check_size(upload)
    check_extension(upload)
    try:
        return require_upload(upload, ALLOWED_IMAGE_TYPES)
    except ValidationError as error:
        raise ImageRejectedError(WRONG_FORMAT) from error


def without_metadata(upload):
    try:
        return core_images.without_metadata(upload)
    except core_images.ImageMetadataError as error:
        raise ImageRejectedError(str(error)) from error
