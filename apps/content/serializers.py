from rest_framework import serializers

from apps.content.models import FAQ, Testimonial


class FAQSerializer(serializers.ModelSerializer):
    class Meta:
        model = FAQ
        fields = ["id", "question", "answer", "sort_order"]


class TestimonialSerializer(serializers.ModelSerializer):
    avatar_url = serializers.CharField(
        read_only=True,
        allow_null=True,
        help_text="The avatar's URL, or null where staff uploaded none.",
    )

    class Meta:
        model = Testimonial
        fields = ["id", "first_name", "age_range", "quote", "rating", "avatar_url", "sort_order"]
