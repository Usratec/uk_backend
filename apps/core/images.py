import logging
from io import BytesIO
from typing import Any

from django.core.files.base import ContentFile, File

logger = logging.getLogger(__name__)

STRIPPABLE_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "TIFF"})

KEPT_INFO_KEYS = frozenset(
    {
        "transparency",
        "gamma",
        "dpi",
        "icc_profile",
        "srgb",
        "compression",
        "resolution",
        "loop",
        "background",
        "duration",
    }
)

ENCODER_KEYS = {
    "JPEG": ("icc_profile", "dpi"),
    "PNG": ("icc_profile", "dpi", "transparency"),
    "WEBP": ("icc_profile", "loop", "duration", "background"),
    "TIFF": ("icc_profile", "dpi", "compression", "resolution"),
}

ORIENTATION_TAG = 0x0112

UPRIGHT = (0, 1)

REENCODE_QUALITY = 95

SANITIZED_ATTR = "_metadata_stripped"


class ImageMetadataError(Exception):
    """The picture could not be rewritten without its metadata, so it must not be stored."""


class SanitizedImageFile(ContentFile):
    def __init__(self, content: bytes, name: str | None, content_type: str = "") -> None:
        super().__init__(content, name=name)
        self.content_type = content_type
        setattr(self, SANITIZED_ATTR, True)


def is_sanitized(upload: File) -> bool:
    return bool(getattr(upload, SANITIZED_ATTR, False))


def without_metadata(upload: File) -> File:
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(upload) as image:
            image_format = image.format or ""
            if image_format not in STRIPPABLE_FORMATS:
                return _marked(upload)

            frames = getattr(image, "n_frames", 1)
            image.load()
            orientation = image.getexif().get(ORIENTATION_TAG, 1)
            upright = orientation in UPRIGHT
            kept = {key: value for key, value in image.info.items() if key in KEPT_INFO_KEYS}

            options: dict[str, Any] = {}
            if frames > 1:
                sequence, durations = _upright_frames(image, orientation)
                rewritten = sequence[0]
                options["save_all"] = True
                options["append_images"] = sequence[1:]
                if any(durations):
                    options["duration"] = durations
            elif upright:
                rewritten = image
            else:
                rewritten = ImageOps.exif_transpose(image)

            rewritten.info = dict(kept)

            for key in ENCODER_KEYS.get(image_format, ()):
                if key in kept and key not in options:
                    options[key] = kept[key]

            if image_format == "JPEG":
                if upright and frames == 1:
                    options["quality"] = "keep"
                else:
                    options["quality"] = REENCODE_QUALITY
                    options["optimize"] = True
            elif image_format == "WEBP":
                options.setdefault("quality", REENCODE_QUALITY)

            buffer = BytesIO()
            rewritten.save(buffer, format=image_format, **options)
    except UnidentifiedImageError as error:
        raise ImageMetadataError("That file is not a picture this site can process.") from error
    except Exception as error:
        raise ImageMetadataError("That picture could not be processed. Please try another file.") from error
    finally:
        upload.seek(0)

    return SanitizedImageFile(buffer.getvalue(), upload.name, getattr(upload, "content_type", "") or "")


def without_metadata_if_image(upload: File) -> File:
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(upload):
            pass
    except UnidentifiedImageError:
        return upload
    except Exception:
        return upload
    finally:
        upload.seek(0)

    return without_metadata(upload)


def _upright_frames(image, orientation: int):
    from PIL import Image, ImageSequence

    method = {
        2: Image.Transpose.FLIP_LEFT_RIGHT,
        3: Image.Transpose.ROTATE_180,
        4: Image.Transpose.FLIP_TOP_BOTTOM,
        5: Image.Transpose.TRANSPOSE,
        6: Image.Transpose.ROTATE_270,
        7: Image.Transpose.TRANSVERSE,
        8: Image.Transpose.ROTATE_90,
    }.get(orientation)

    sequence = []
    durations = []
    for frame in ImageSequence.Iterator(image):
        copied = frame.copy()
        durations.append(frame.info.get("duration", 0))
        copied.info = {}
        sequence.append(copied if method is None else copied.transpose(method))
    return sequence, durations


def _marked(upload: File) -> File:
    try:
        setattr(upload, SANITIZED_ATTR, True)
    except AttributeError:
        logger.warning("Could not mark %s as stripped; it may be rewritten twice", type(upload).__name__)
    return upload
