"""An upload's type is whatever its bytes are."""

from io import BytesIO

import pytest
from django.core.exceptions import ValidationError

from apps.core.uploads import identify


class Named(BytesIO):
    content_type = "application/octet-stream"


def test_a_png_is_a_png_whatever_it_was_called():
    from apps.learning.tests.conftest import png_bytes

    upload = Named(png_bytes())
    upload.content_type = "text/html"

    assert identify(upload) == "image/png"
    assert upload.content_type == "image/png"


def test_html_is_refused():
    with pytest.raises(ValidationError):
        identify(Named(b"<html><script>alert(1)</script></html>"))


def test_an_svg_without_script_is_accepted():
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"

    assert identify(Named(svg)) == "image/svg+xml"


def test_an_svg_with_script_is_refused():
    svg = b"<svg><script>alert(1)</script></svg>"

    with pytest.raises(ValidationError):
        identify(Named(svg))


def test_an_svg_bigger_than_the_markup_check_is_refused():
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'>" + (b" " * (512 * 1024)) + b"</svg>"

    with pytest.raises(ValidationError):
        identify(Named(svg))


def test_an_svg_with_script_past_the_first_slice_is_refused():
    svg = (
        b"<svg xmlns='http://www.w3.org/2000/svg'>"
        + (b" " * 65536)
        + b"<script>alert(1)</script></svg>"
    )

    with pytest.raises(ValidationError):
        identify(Named(svg))


def test_an_svg_at_the_markup_check_limit_is_accepted():
    opening = b"<svg xmlns='http://www.w3.org/2000/svg'>"
    closing = b"</svg>"
    svg = opening + (b" " * (512 * 1024 - len(opening) - len(closing))) + closing

    assert identify(Named(svg)) == "image/svg+xml"


def test_a_pdf_is_not_accepted_as_a_picture():
    from apps.core.uploads import RASTER_IMAGE_TYPES, require_upload

    with pytest.raises(ValidationError):
        require_upload(Named(b"%PDF-1.4\n"), RASTER_IMAGE_TYPES)


def test_a_document_is_stored_to_be_downloaded():
    from apps.core import uploads
    from apps.core.storage import SafeS3Storage

    storage = object.__new__(SafeS3Storage)
    storage.object_parameters = {}
    token = uploads.begin_stored_upload(Named(b"%PDF-1.4\n"), "learning/document/notes.pdf")
    try:
        params = SafeS3Storage.get_object_parameters(storage, "notes.pdf")
    finally:
        uploads.finish_stored_upload(token)

    assert params["ContentDisposition"] == 'attachment; filename="notes.pdf"'


def test_a_video_is_stored_so_the_player_can_open_it():
    from apps.core import uploads
    from apps.core.storage import SafeS3Storage
    from apps.learning.tests.conftest import mp4_bytes

    storage = object.__new__(SafeS3Storage)
    storage.object_parameters = {}
    token = uploads.begin_stored_upload(Named(mp4_bytes()), "learning/video/clip.mp4")
    try:
        params = SafeS3Storage.get_object_parameters(storage, "clip.mp4")
    finally:
        uploads.finish_stored_upload(token)

    assert params["ContentDisposition"] == 'inline; filename="clip.mp4"'
