from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any, Optional

from decouple import config
from redis import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_redis_client() -> Optional[Redis]:
    url = config("REDIS_URL", default=None)
    if url:
        return Redis.from_url(str(url), decode_responses=True)

    host = config("REDIS_HOST", default=None)
    if host:
        port = config("REDIS_PORT", default=6379, cast=int)
        db = config("REDIS_DB", default=0, cast=int)
        return Redis(host=str(host), port=port, db=db, decode_responses=True)

    return None


def redis_get_json(client: Optional[Redis], key: str) -> Optional[Any]:
    if client is None:
        return None
    try:
        raw = client.get(key)
    except RedisError as exc:
        logger.debug("Failed to read %s from redis: %s", key, exc)
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.debug("Invalid JSON cached at %s", key)
        return None


def redis_set_json(client: Optional[Redis], key: str, value: Any, ttl: int) -> None:
    if client is None:
        return
    try:
        payload = json.dumps(value)
    except TypeError as exc:
        logger.debug("Failed to serialize %s for redis cache: %s", key, exc)
        return
    try:
        client.setex(key, ttl, payload)
    except RedisError as exc:
        logger.debug("Failed to write %s to redis: %s", key, exc)
