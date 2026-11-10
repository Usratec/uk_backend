from importlib import import_module
from types import SimpleNamespace
from urllib.parse import urlsplit

from django.apps import apps as global_apps
from django.conf import settings
from django.contrib.sitemaps import Sitemap


class FrontendSitemap(Sitemap):
    def get_urls(self, page: int | str = 1, site=None, protocol: str | None = None) -> list[dict]:
        frontend = urlsplit(settings.FRONTEND_BASE_URL)
        return super().get_urls(
            page=page,
            site=SimpleNamespace(domain=frontend.netloc, name=frontend.netloc),  # type: ignore[arg-type]
            protocol=frontend.scheme or protocol,
        )


def declared_sitemaps() -> dict[str, type[Sitemap]]:
    sitemaps: dict[str, type[Sitemap]] = {}

    for app_config in global_apps.get_app_configs():
        try:
            module = import_module(f"{app_config.name}.sitemaps")
        except ModuleNotFoundError:
            continue
        sitemaps.update(getattr(module, "SITEMAPS", {}))

    return sitemaps
