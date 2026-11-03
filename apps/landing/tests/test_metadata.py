import pytest
from PIL import Image

from apps.core.tests.test_images import GPS_IFD, MAKE, photo_with_gps
from apps.landing.models import LandingMediaType
from apps.landing.tests.conftest import make_media, uploaded_video

pytestmark = pytest.mark.django_db


class TestLandingUploads:
    def test_an_image_loses_its_location(self):
        media = make_media(file=photo_with_gps("JPEG"))

        with Image.open(media.file) as image:
            assert dict(image.getexif().get_ifd(GPS_IFD)) == {}
            assert MAKE not in dict(image.getexif())

    def test_a_poster_loses_its_location(self):
        media = make_media(
            media_type=LandingMediaType.VIDEO,
            file=uploaded_video(),
            poster=photo_with_gps("PNG"),
        )

        with Image.open(media.poster) as image:
            assert dict(image.getexif()) == {}

    def test_a_video_is_stored_byte_for_byte(self):
        upload = uploaded_video()
        sent = upload.read()
        upload.seek(0)

        media = make_media(media_type=LandingMediaType.VIDEO, file=upload)

        assert media.file.read() == sent
