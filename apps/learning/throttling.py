from rest_framework.throttling import AnonRateThrottle


class EbookSampleRequestThrottle(AnonRateThrottle):
    """Asking for the free chapter, per IP address. Each request sends an email to any address given."""

    scope = "ebook_sample_request"
