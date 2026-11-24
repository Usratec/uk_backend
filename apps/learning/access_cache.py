import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

TTL_SECONDS = 60

NAMESPACE = "video_access"


def decision_key(*, user_id: int, asset_id: int, generation: int) -> str:
    return f"{NAMESPACE}:{user_id}:{asset_id}:{generation}"


def generation_key(user_id: int) -> str:
    return f"{NAMESPACE}:generation:{user_id}"


def decide(*, user, asset, check) -> bool:
    user_id = getattr(user, "pk", None)
    asset_id = getattr(asset, "pk", None)
    if user_id is None or asset_id is None or not getattr(user, "is_authenticated", False):
        return check()

    generation = _generation_of(user_id)
    if generation is None:
        return check()

    key = decision_key(user_id=user_id, asset_id=asset_id, generation=generation)
    remembered = _read(key)
    if remembered is not None:
        return remembered

    allowed = check()
    _write(key, allowed)
    return allowed


def forget(user_id: int) -> None:
    """Throw away everything remembered about one student, because their access has changed."""
    key = generation_key(user_id)
    try:
        cache.incr(key)
    except ValueError:
        try:
            cache.set(key, 1, None)
        except Exception:
            logger.exception("Could not reset the video access generation for user %s", user_id)
    except Exception:
        logger.exception("Could not clear the cached video access of user %s", user_id)


def _generation_of(user_id: int) -> int | None:
    try:
        value = cache.get_or_set(generation_key(user_id), 0, None)
    except Exception:
        logger.exception("Could not read the video access generation of user %s", user_id)
        return None
    return value if isinstance(value, int) else None


def _read(key: str) -> bool | None:
    try:
        value = cache.get(key)
    except Exception:
        logger.exception("Could not read a cached video access decision")
        return None
    return value if isinstance(value, bool) else None


def _write(key: str, allowed: bool) -> None:
    try:
        cache.set(key, allowed, TTL_SECONDS)
    except Exception:
        logger.exception("Could not remember a video access decision")
