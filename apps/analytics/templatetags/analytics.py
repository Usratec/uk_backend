from urllib.parse import urlencode

from django import template

register = template.Library()


@register.simple_tag
def filter_query(filters, **extra) -> str:
    query = {**filters.as_query(), **{key: value for key, value in extra.items() if value}}
    return urlencode(query)


@register.filter
def percent(value) -> str:
    return "—" if value is None else f"{value:.1f}%"
