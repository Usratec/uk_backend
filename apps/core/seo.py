from dataclasses import dataclass
from urllib.parse import urljoin

from django.conf import settings

CONTEXT = "https://schema.org"


@dataclass(frozen=True)
class Crumb:
    name: str
    url: str | None = None


def site_url(path: str = "/") -> str:
    return urljoin(settings.FRONTEND_BASE_URL, path.lstrip("/"))


def breadcrumb_list(crumbs: list[Crumb]) -> dict | None:
    if len(crumbs) < 2:
        return None

    return {
        "@context": CONTEXT,
        "@type": "BreadcrumbList",
        "itemListElement": [_list_item(crumb, position) for position, crumb in enumerate(crumbs, start=1)],
    }


def _list_item(crumb: Crumb, position: int) -> dict:
    item = {"@type": "ListItem", "position": position, "name": crumb.name}
    if crumb.url:
        item["item"] = crumb.url
    return item
