from rest_framework.throttling import AnonRateThrottle, UserRateThrottle


class ContactAnonThrottle(AnonRateThrottle):
    scope = "contact_anon"


class ContactUserThrottle(UserRateThrottle):
    scope = "contact_user"
