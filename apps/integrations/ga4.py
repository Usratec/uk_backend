import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

COLLECT_URL = "https://www.google-analytics.com/mp/collect"

TIMEOUT = (3.05, 5.0)

REJECTED_STATUSES = frozenset({400, 401, 403, 404})


class Ga4Error(Exception):
    """GA4 did not accept an event."""


class Ga4NotDeliveredError(Ga4Error):
    """The event certainly did not reach GA4, so sending it again cannot double-count anything."""


class Ga4UnconfirmedError(Ga4Error):
    """GA4 may or may not have recorded the event, so sending it again could double-count it."""


def configured() -> bool:
    return bool(settings.GA4_MEASUREMENT_ID and settings.GA4_API_SECRET)


def send_event(payload: dict) -> None:
    response = _post(payload)

    if response.status_code in REJECTED_STATUSES:
        raise Ga4NotDeliveredError(f"GA4 refused the event (HTTP {response.status_code})")

    if not response.ok:
        raise Ga4UnconfirmedError(f"GA4 answered HTTP {response.status_code}")


def _post(payload: dict) -> requests.Response:
    if not configured():
        missing = "GA4_MEASUREMENT_ID" if not settings.GA4_MEASUREMENT_ID else "GA4_API_SECRET"
        raise Ga4NotDeliveredError(f"GA4 is not configured: {missing} is empty")

    try:
        return requests.post(
            COLLECT_URL,
            params={"measurement_id": settings.GA4_MEASUREMENT_ID, "api_secret": settings.GA4_API_SECRET},
            json=payload,
            timeout=TIMEOUT,
            allow_redirects=False,
        )
    except requests.ReadTimeout as error:
        raise Ga4UnconfirmedError("GA4 did not answer in time") from error
    except (requests.ConnectionError, requests.ConnectTimeout) as error:
        raise Ga4NotDeliveredError("GA4 could not be reached") from error
    except requests.RequestException as error:
        raise Ga4UnconfirmedError("The GA4 request failed") from error
