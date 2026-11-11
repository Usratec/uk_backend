from datetime import datetime

from apps.blog import services
from apps.core.sitemaps import FrontendSitemap


class BlogPostSitemap(FrontendSitemap):
    changefreq = "weekly"
    priority = 0.6

    def items(self) -> list[services.SitemapEntry]:
        return services.sitemap_entries()

    def location(self, item: services.SitemapEntry) -> str:
        return item.path

    def lastmod(self, item: services.SitemapEntry) -> datetime:
        return item.lastmod


SITEMAPS = {"blog": BlogPostSitemap}
