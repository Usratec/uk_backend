import re

import nh3

ALLOWED_TAGS = {
    "p",
    "br",
    "strong",
    "b",
    "em",
    "i",
    "u",
    "s",
    "sub",
    "sup",
    "h2",
    "h3",
    "h4",
    "ul",
    "ol",
    "li",
    "blockquote",
    "a",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "figure",
    "figcaption",
    "img",
    "hr",
    "code",
    "pre",
}

ALLOWED_ATTRIBUTES = {
    "a": {"href", "title", "target"},
    "img": {"src", "alt", "width", "height"},
    "th": {"colspan", "rowspan", "scope"},
    "td": {"colspan", "rowspan"},
    "figure": {"class"},
    "table": {"class"},
}

ALLOWED_SCHEMES = {"http", "https", "mailto"}

IMAGE_REFERENCE = "/api/blog/images/{key}/"

EMBEDDED_MEDIA = re.compile(r"<(?:img|figure|video|audio|iframe)\b", re.IGNORECASE)


def reference_for(key) -> str:
    return IMAGE_REFERENCE.format(key=key)


def clean(html: str | None) -> str:
    if not html:
        return ""

    return nh3.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes={tag: set(attributes) for tag, attributes in ALLOWED_ATTRIBUTES.items()},
        url_schemes=ALLOWED_SCHEMES,
        link_rel="noopener noreferrer",
    )


def is_empty(html: str | None) -> bool:
    if not html:
        return True

    if EMBEDDED_MEDIA.search(html):
        return False

    return not nh3.clean(html, tags=set()).replace("&nbsp;", " ").strip()
