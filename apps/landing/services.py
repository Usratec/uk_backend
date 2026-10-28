from apps.landing.models import LandingMedia, LandingSection, LandingText


def text_values() -> dict[str, str]:
    return dict(LandingText.objects.values_list("key", "value"))


def media_slots() -> dict[str, LandingMedia | None]:
    slots: dict[str, LandingMedia | None] = dict.fromkeys(LandingSection.values)
    for media in LandingMedia.objects.filter(is_active=True).exclude(section=""):
        if media.section in slots and slots[media.section] is None:
            slots[media.section] = media
    return slots
