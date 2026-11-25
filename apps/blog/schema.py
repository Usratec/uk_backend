from datetime import datetime
from urllib.parse import urljoin

from django.conf import settings
from django.urls import reverse

from apps.blog import html, services
from apps.blog.models import BlogPost, BlogPostFAQ
from apps.core.seo import CONTEXT, Crumb, breadcrumb_list, site_url

DESCRIPTION_FALLBACK_LENGTH = 200

HOME_LABEL = "Home"
BLOG_LABEL = "Blog"


def structured_data_for(post: BlogPost, *, request=None) -> list[dict]:
    graphs = [article(post, request=request)]

    questions = faq_page(post)
    if questions is not None:
        graphs.append(questions)

    trail = breadcrumbs(post)
    if trail is not None:
        graphs.append(trail)

    return graphs


def article(post: BlogPost, *, request=None) -> dict:
    canonical = services.canonical_of(post)

    data: dict = {
        "@context": CONTEXT,
        "@type": "Article",
        "headline": post.meta_title or post.title,
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
        "url": canonical,
    }

    description = _description(post)
    if description:
        data["description"] = description

    image = _image_url(post, request=request)
    if image:
        data["image"] = [image]

    if post.publish_at is not None:
        data["datePublished"] = _timestamp(post.publish_at)
    if post.updated_at is not None:
        data["dateModified"] = _timestamp(post.updated_at)

    if post.byline:
        data["author"] = {"@type": "Person", "name": post.byline}

    return data


def _description(post: BlogPost) -> str:
    if post.meta_description:
        return post.meta_description
    if post.excerpt:
        return post.excerpt

    text = " ".join(html.plain_text(post.body_html).split())
    if len(text) <= DESCRIPTION_FALLBACK_LENGTH:
        return text
    return text[:DESCRIPTION_FALLBACK_LENGTH].rsplit(" ", 1)[0] + "…"


def _image_url(post: BlogPost, *, request=None) -> str | None:
    if post.social_image:
        path = reverse("blog-post-social-image", args=[post.slug])
    elif post.cover_image:
        path = reverse("blog-post-cover-image", args=[post.slug])
    else:
        return None

    if request is not None:
        return request.build_absolute_uri(path)
    return urljoin(settings.BACKEND_BASE_URL, path.lstrip("/"))


def _timestamp(moment: datetime) -> str:
    return moment.isoformat()


def faq_page(post: BlogPost) -> dict | None:
    questions = services.faqs_of(post)
    if not questions:
        return None

    return {
        "@context": CONTEXT,
        "@type": "FAQPage",
        "mainEntity": [_question(faq) for faq in questions],
    }


def _question(faq: BlogPostFAQ) -> dict:
    return {
        "@type": "Question",
        "name": faq.question.strip(),
        "acceptedAnswer": {"@type": "Answer", "text": faq.answer.strip()},
    }


def breadcrumbs(post: BlogPost) -> dict | None:
    return breadcrumb_list(
        [
            Crumb(name=HOME_LABEL, url=site_url()),
            Crumb(name=BLOG_LABEL, url=site_url(services.BLOG_PATH)),
            Crumb(name=post.title),
        ]
    )
