"""What an upload actually is, read from its bytes rather than from the type the browser sent.

The type a browser declares is whatever the sender put on the request. These helpers decide from
the file itself, and they are the only types the site will store or hand back to a browser.
"""

import os
from contextvars import ContextVar

from django.core.exceptions import ValidationError

# Raster pictures the players and pages draw. SVG is separate: it can carry script.
RASTER_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
VIDEO_TYPES = frozenset({"video/mp4", "video/webm", "video/quicktime"})
DOCUMENT_TYPES = frozenset({"application/pdf"})
SUBTITLE_TYPES = frozenset({"text/vtt"})
SVG_TYPE = "image/svg+xml"

# Everything that may be written to the bucket. A file that matches none of these is refused.
STORABLE_TYPES = RASTER_IMAGE_TYPES | VIDEO_TYPES | DOCUMENT_TYPES | SUBTITLE_TYPES | {SVG_TYPE}

# Played or drawn by the browser. Anything else is downloaded, so it is not rendered as a page.
INLINE_TYPES = RASTER_IMAGE_TYPES | VIDEO_TYPES | SUBTITLE_TYPES

# Set while a file is being written, so the bucket can record how the browser should treat it.
_content_disposition: ContextVar[str | None] = ContextVar("upload_content_disposition", default=None)

_SVG_ACTIVE = ("<script", "javascript:", "onload=", "onerror=", "onclick=", "onmouseover=")

_SIGNATURES = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"%PDF-", "application/pdf"),
    (b"\x1a\x45\xdf\xa3", "video/webm"),
)

# Past the first slice, far enough to see a script, and small enough to keep in a worker.
_MARKUP_READ_LIMIT = 512 * 1024


def read_header(content, limit: int = 65536) -> bytes:
    content.seek(0)
    header = content.read(limit)
    if header.lstrip().startswith(b"<"):
        # Markup is small. Read further so a script cannot sit past the first slice,
        # and stop so a huge upload cannot fill the worker.
        content.seek(0)
        header = content.read(_MARKUP_READ_LIMIT)
        if content.read(1):
            content.seek(0)
            raise ValidationError("This file is too large to check.")
    content.seek(0)
    return header


def sniff_content_type(header: bytes) -> str | None:
    """The type the bytes actually are, or None when they are not a format we store."""
    for signature, content_type in _SIGNATURES:
        if header.startswith(signature):
            return content_type
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        if header[8:12] == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    if _is_webvtt(header):
        return "text/vtt"
    return _svg_type(header)


def identify(content) -> str:
    """The type of this upload. Raises ValidationError when the bytes are not allowed."""
    found = sniff_content_type(read_header(content))
    if found not in STORABLE_TYPES:
        raise ValidationError("This file is not a format the site can store.")
    # FieldFile wraps the upload. The wrapper is what model.save sees; storage is given the
    # inner file, so the sniffed type has to be on that inner file or the bucket keeps the
    # browser's type.
    target = getattr(content, "file", content)
    if hasattr(target, "content_type"):
        target.content_type = found
    if target is not content and hasattr(content, "content_type"):
        content.content_type = found
    return found


def require_upload(content, allowed: frozenset[str]) -> str:
    """Like identify, and the type also has to be one this field accepts."""
    found = identify(content)
    if found not in allowed:
        raise ValidationError(f"This file is {found}, which is not allowed here.")
    return found


def begin_stored_upload(content, name: str):
    """Check the bytes and remember how the stored object should be delivered. Returns a token."""
    found = identify(content)
    filename = os.path.basename(name).replace('"', "").replace("\r", "").replace("\n", "") or "download"
    kind = "inline" if found in INLINE_TYPES else "attachment"
    return _content_disposition.set(f'{kind}; filename="{filename}"')


def finish_stored_upload(token) -> None:
    _content_disposition.reset(token)


def content_disposition() -> str | None:
    return _content_disposition.get()


def delivery_headers(content_type: str, filename: str) -> dict[str, str]:
    """Headers for a file this process sends itself, rather than the bucket."""
    safe_type = content_type if content_type in STORABLE_TYPES else "application/octet-stream"
    name = os.path.basename(filename).replace('"', "").replace("\r", "").replace("\n", "") or "download"
    kind = "inline" if safe_type in INLINE_TYPES else "attachment"
    return {
        "Content-Type": safe_type,
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'{kind}; filename="{name}"',
    }


def _is_webvtt(header: bytes) -> bool:
    text = header.decode("utf-8-sig", errors="replace").lstrip("\ufeff")
    line = text.partition("\n")[0].rstrip("\r")
    return line == "WEBVTT" or line.startswith("WEBVTT ") or line.startswith("WEBVTT\t")


def _svg_type(header: bytes) -> str | None:
    try:
        text = header.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    lowered = text.lstrip().lower()
    if not (lowered.startswith("<svg") or (lowered.startswith("<?xml") and "<svg" in lowered)):
        return None
    if any(token in lowered for token in _SVG_ACTIVE):
        return None
    return SVG_TYPE
