import re

import nh3

# What a lesson or chapter may contain. Headings start at h2, because the page itself provides
# the h1, and a second h1 in the content would confuse screen readers and search engines.
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
    # The editor marks image and table alignment with a class.
    "figure": {"class"},
    "table": {"class"},
}

# Only links that go somewhere safe. This is what stops `javascript:` links.
ALLOWED_SCHEMES = {"http", "https", "mailto"}

MEDIA_REFERENCE = "/api/learning/media/{pk}/"
MEDIA_REFERENCE_PATTERN = re.compile(r'src="/api/learning/media/(?P<pk>\d+)/"')

EMBEDDED_MEDIA = re.compile(r"<(?:img|figure|video|audio|iframe)\b", re.IGNORECASE)

SIGN_MARKER = re.compile(
    r"(?:<p>\s*)?\[signs:\s*(?P<codes>[^\]|]+?)\s*(?:\|\s*(?P<caption>[^\]]*?))?\s*\](?:\s*</p>)?",
    re.IGNORECASE,
)


def split_on_signs(html: str | None) -> list[dict]:
    if not html:
        return []

    pieces: list[dict] = []
    position = 0

    for marker in SIGN_MARKER.finditer(html):
        _add_text(pieces, html[position : marker.start()])
        pieces.append(
            {
                "codes": [code.strip() for code in marker.group("codes").split(",") if code.strip()],
                "caption": (marker.group("caption") or "").strip(),
            }
        )
        position = marker.end()

    _add_text(pieces, html[position:])
    return pieces


def _add_text(pieces: list[dict], html: str) -> None:
    if not is_empty(html):
        pieces.append({"html": html.strip()})


def sign_codes_in(html: str | None) -> list[str]:
    return [code for piece in split_on_signs(html) for code in piece.get("codes", [])]


def reference_for(asset_id: int) -> str:
    return MEDIA_REFERENCE.format(pk=asset_id)


def media_ids_in(html: str | None) -> list[int]:
    if not html:
        return []

    return list(dict.fromkeys(int(match.group("pk")) for match in MEDIA_REFERENCE_PATTERN.finditer(html)))


def with_media_urls(html: str | None, urls: dict[int, str]) -> str:
    if not html:
        return ""

    def swap(match: re.Match) -> str:
        url = urls.get(int(match.group("pk")), "")
        return f'src="{url}"'

    return MEDIA_REFERENCE_PATTERN.sub(swap, html)


def clean(html: str | None) -> str:
    """Return the HTML with anything we do not allow removed."""
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
    """Whether this is really empty, seeing past the markup an editor leaves behind.

    A rich-text editor rarely hands back an empty string: pressing a key and deleting it leaves
    something like ``<p>&nbsp;</p>``, which looks blank to staff and should count as blank here.
    """
    if not html:
        return True

    if EMBEDDED_MEDIA.search(html):
        return False

    text = nh3.clean(html, tags=set()).replace("&nbsp;", " ")
    return not text.strip()
