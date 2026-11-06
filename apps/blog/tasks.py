import logging

from celery import shared_task

from apps.blog import services

logger = logging.getLogger(__name__)


@shared_task
def publish_scheduled_posts() -> int:
    published = services.publish_due_posts()
    if published:
        logger.info("Published %d scheduled blog post(s)", published)
    return published
