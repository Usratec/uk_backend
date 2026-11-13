from rest_framework import serializers

from apps.content.models import FAQ, ContactSubmission, ContactTopic, Testimonial


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


class ContactMessageSerializer(serializers.Serializer):
    name = serializers.CharField(min_length=2, max_length=60, trim_whitespace=True)
    email = serializers.EmailField(max_length=254)
    topic = serializers.ChoiceField(choices=ContactTopic.choices, required=False, allow_blank=True, default="")
    message = serializers.CharField(
        max_length=ContactSubmission.MESSAGE_MAX_LENGTH,
        trim_whitespace=True,
        allow_blank=False,
    )

    def validate_email(self, value: str) -> str:
        return value.strip()

    def validate_message(self, value: str) -> str:
        if not value.strip():
            raise serializers.ValidationError("This field may not be blank.")
        return value.strip()


class ContactMessageResponseSerializer(serializers.Serializer):
    message = serializers.CharField()
