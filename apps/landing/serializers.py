from rest_framework import serializers

from apps.landing.models import LandingMedia


class LandingMediaSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField(help_text="Absolute URL of the image or video.")
    poster_url = serializers.SerializerMethodField(
        help_text="Absolute URL of the still shown before a video plays, or null.",
    )

    class Meta:
        model = LandingMedia
        fields = ["media_type", "url", "poster_url", "alt_text", "title"]

    def _absolute(self, file) -> str | None:
        if not file:
            return None
        url = file.url
        request = self.context.get("request")
        return request.build_absolute_uri(url) if request is not None else url

    def get_url(self, media: LandingMedia) -> str | None:
        return self._absolute(media.file)

    def get_poster_url(self, media: LandingMedia) -> str | None:
        return self._absolute(media.poster)


class LandingMediaMapSerializer(serializers.Serializer):
    hero_frame = LandingMediaSerializer(allow_null=True)
    hero_background = LandingMediaSerializer(allow_null=True)
    feature_hazard_perception = LandingMediaSerializer(allow_null=True)
    feature_video_lessons = LandingMediaSerializer(allow_null=True)
    feature_practice_tests = LandingMediaSerializer(allow_null=True)
    feature_ebook = LandingMediaSerializer(allow_null=True)


class LandingTextMapSerializer(serializers.Serializer):
    def to_representation(self, instance: dict[str, str]) -> dict[str, str]:
        return dict(instance)


class LandingContentSerializer(serializers.Serializer):
    text = serializers.DictField(child=serializers.CharField())
    media = LandingMediaMapSerializer()
