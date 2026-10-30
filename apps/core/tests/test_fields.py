import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.core.tests.test_images import GPS_IFD, MAKE, photo_with_gps
from apps.learning.models import MediaAsset, MediaKind

pytestmark = pytest.mark.django_db

VIDEO_BYTES = b"\x00\x00\x00 ftypisom and the rest is not a picture"


class TestSanitizedFileField:
    def test_an_image_saved_through_the_model_loses_its_metadata(self):
        asset = MediaAsset(title="Junction", kind=MediaKind.IMAGE, file=photo_with_gps("JPEG"))

        asset.save()

        with Image.open(asset.file) as image:
            assert dict(image.getexif()) == {}
            assert dict(image.getexif().get_ifd(GPS_IFD)) == {}

    def test_a_video_saved_through_the_model_is_stored_byte_for_byte(self):
        upload = SimpleUploadedFile("clip.mp4", VIDEO_BYTES, content_type="video/mp4")
        asset = MediaAsset(title="Clip", kind=MediaKind.VIDEO, file=upload)

        asset.save()

        assert asset.file.read() == VIDEO_BYTES

    def test_the_recorded_size_is_the_size_of_what_was_stored(self):
        upload = photo_with_gps("JPEG")
        sent = upload.size
        asset = MediaAsset(title="Junction", kind=MediaKind.IMAGE, file=upload)

        asset.save()

        assert asset.size_bytes == len(asset.file.read())
        assert asset.size_bytes != sent

    def test_the_recorded_size_of_a_video_is_the_size_of_the_upload(self):
        upload = SimpleUploadedFile("clip.mp4", VIDEO_BYTES, content_type="video/mp4")
        asset = MediaAsset(title="Clip", kind=MediaKind.VIDEO, file=upload)

        asset.save()

        assert asset.size_bytes == len(VIDEO_BYTES)

    def test_the_declared_content_type_is_still_recorded(self):
        asset = MediaAsset(title="Junction", kind=MediaKind.IMAGE, file=photo_with_gps("PNG"))

        asset.save()

        assert asset.content_type == "image/png"

    def test_saving_the_row_again_does_not_rewrite_the_file(self):
        asset = MediaAsset(title="Junction", kind=MediaKind.IMAGE, file=photo_with_gps("JPEG"))
        asset.save()
        stored = asset.file.name
        first = asset.file.read()

        asset.title = "Junction, renamed"
        asset.save()

        assert asset.file.name == stored
        asset.file.seek(0)
        assert asset.file.read() == first


class TestSanitizedImageField:
    def test_an_avatar_saved_through_the_model_loses_its_metadata(self):
        from apps.content.models import Testimonial

        testimonial = Testimonial(first_name="Ellie", quote="It helped.", rating=5, avatar=photo_with_gps("JPEG"))

        testimonial.save()

        with Image.open(testimonial.avatar) as image:
            assert MAKE not in dict(image.getexif())
            assert dict(image.getexif().get_ifd(GPS_IFD)) == {}
