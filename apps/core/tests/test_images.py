from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from apps.core.images import SanitizedImageFile, without_metadata, without_metadata_if_image

GPS_IFD = 0x8825
ORIENTATION = 0x0112
MAKE = 0x010F
TIMESTAMP = 0x0132


def locating_exif(orientation: int = 1):
    from PIL import Image

    exif = Image.Exif()
    exif[MAKE] = "ProbeCam"
    exif[TIMESTAMP] = "2026:01:01 10:00:00"
    exif[ORIENTATION] = orientation
    gps = exif.get_ifd(GPS_IFD)
    gps[1] = "N"
    gps[2] = (51.0, 30.0, 0.0)
    gps[3] = "W"
    gps[4] = (0.0, 7.0, 0.0)
    return exif


def photo_with_gps(image_format: str = "JPEG", orientation: int = 1, size: tuple[int, int] = (40, 20), **options):
    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=image_format, exif=locating_exif(orientation), **options)
    extension = "jpg" if image_format == "JPEG" else image_format.lower()
    content_type = "image/jpeg" if image_format == "JPEG" else f"image/{extension}"
    return SimpleUploadedFile(f"me.{extension}", buffer.getvalue(), content_type=content_type)


def animation_with_gps(orientation: int = 1, size: tuple[int, int] = (8, 8), loop: int = 0, duration: int = 100):
    from PIL import Image

    frames = [Image.new("RGB", size, colour) for colour in ("red", "green", "blue")]
    buffer = BytesIO()
    frames[0].save(
        buffer,
        format="WEBP",
        save_all=True,
        append_images=frames[1:],
        duration=duration,
        loop=loop,
        exif=locating_exif(orientation),
    )
    return SimpleUploadedFile("me.webp", buffer.getvalue(), content_type="image/webp")


def srgb_profile() -> bytes:
    from PIL import ImageCms

    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def opened(stored):
    from PIL import Image

    return Image.open(BytesIO(stored.read()))


class TestStripping:
    @pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP"])
    def test_the_gps_fix_does_not_survive(self, image_format):
        upload = photo_with_gps(image_format)

        with opened(without_metadata(upload)) as image:
            assert dict(image.getexif()) == {}
            assert dict(image.getexif().get_ifd(GPS_IFD)) == {}

    @pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP", "TIFF"])
    def test_the_camera_make_and_the_timestamp_do_not_survive(self, image_format):
        with opened(without_metadata(photo_with_gps(image_format))) as image:
            tags = dict(image.getexif())
            assert MAKE not in tags
            assert TIMESTAMP not in tags

    def test_a_png_comment_does_not_survive(self):
        from PIL import Image, PngImagePlugin

        meta = PngImagePlugin.PngInfo()
        meta.add_text("Comment", "taken at home")
        buffer = BytesIO()
        Image.new("RGB", (8, 8), "blue").save(buffer, format="PNG", pnginfo=meta)
        upload = SimpleUploadedFile("me.png", buffer.getvalue(), content_type="image/png")

        with opened(without_metadata(upload)) as image:
            assert "Comment" not in image.info

    def test_the_picture_itself_is_unchanged(self):
        upload = photo_with_gps("PNG", size=(12, 9))

        with opened(without_metadata(upload)) as image:
            assert image.size == (12, 9)
            assert image.format == "PNG"

    def test_an_orientation_tag_is_applied_to_the_pixels_instead(self):
        upload = photo_with_gps("JPEG", orientation=6, size=(40, 20))

        with opened(without_metadata(upload)) as image:
            assert image.size == (20, 40)
            assert ORIENTATION not in dict(image.getexif())

    def test_transparency_is_kept(self):
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGBA", (8, 8), (255, 0, 0, 0)).save(buffer, format="PNG")
        upload = SimpleUploadedFile("me.png", buffer.getvalue(), content_type="image/png")

        with opened(without_metadata(upload)) as image:
            assert image.mode == "RGBA"
            assert image.getpixel((0, 0))[3] == 0

    def test_an_animated_image_keeps_its_frames(self):
        upload = animation_with_gps()

        with opened(without_metadata(upload)) as image:
            assert image.n_frames == 3
            assert dict(image.getexif()) == {}

    def test_an_animated_image_keeps_how_often_it_loops(self):
        upload = animation_with_gps(loop=2)

        with opened(without_metadata(upload)) as image:
            assert image.info["loop"] == 2

    def test_an_animated_image_keeps_how_long_each_frame_lasts(self):
        upload = animation_with_gps(duration=120)

        with opened(without_metadata(upload)) as image:
            image.load()
            assert image.info["duration"] == 120

    def test_a_sideways_animation_is_turned_the_right_way_up(self):
        upload = animation_with_gps(orientation=6, size=(40, 20))

        with opened(without_metadata(upload)) as image:
            assert image.size == (20, 40)
            assert image.n_frames == 3
            assert ORIENTATION not in dict(image.getexif())

    @pytest.mark.parametrize("image_format", ["JPEG", "PNG"])
    def test_the_colour_profile_is_kept(self, image_format):
        upload = photo_with_gps(image_format, icc_profile=srgb_profile())

        with opened(without_metadata(upload)) as image:
            assert image.info.get("icc_profile") == srgb_profile()

    @pytest.mark.parametrize("image_format", ["JPEG", "PNG"])
    def test_the_print_resolution_is_kept(self, image_format):
        upload = photo_with_gps(image_format, dpi=(300, 300))

        with opened(without_metadata(upload)) as image:
            assert [round(axis) for axis in image.info["dpi"]] == [300, 300]

    def test_a_format_we_do_not_rewrite_is_left_alone(self):
        from PIL import Image

        buffer = BytesIO()
        Image.new("RGB", (4, 4), "blue").save(buffer, format="GIF")
        upload = SimpleUploadedFile("me.gif", buffer.getvalue(), content_type="image/gif")

        assert without_metadata(upload) is upload

    def test_the_declared_content_type_survives(self):
        stripped = without_metadata(photo_with_gps("PNG"))

        assert isinstance(stripped, SanitizedImageFile)
        assert stripped.content_type == "image/png"


class TestFilesThatAreNotPictures:
    def test_a_video_is_handed_back_untouched(self):
        upload = SimpleUploadedFile("clip.mp4", b"\x00\x00\x00 ftypisom not a picture", content_type="video/mp4")

        assert without_metadata_if_image(upload) is upload

    def test_a_subtitle_track_is_handed_back_untouched(self):
        upload = SimpleUploadedFile("en.vtt", b"WEBVTT\n\n00:00.000 --> 00:01.000\nhello", content_type="text/vtt")

        assert without_metadata_if_image(upload) is upload

    def test_a_picture_among_them_is_still_stripped(self):
        with opened(without_metadata_if_image(photo_with_gps())) as image:
            assert dict(image.getexif()) == {}
