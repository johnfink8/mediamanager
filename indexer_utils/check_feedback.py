from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .redis_client import get_redis_client, redis_get_json, redis_set_json

logger = logging.getLogger(__name__)

CHECK_HISTORY_TTL_SECONDS = 60 * 60 * 24 * 14


def _history_key(kind: str) -> str:
    return f"check-history:{kind}"


def _write_history(kind: str, entry: Dict[str, Any]) -> None:
    client = get_redis_client()
    if client is None:
        logger.debug("Skipping redis write for %s check; redis unavailable", kind)
        return

    history: List[Dict[str, Any]] = redis_get_json(client, _history_key(kind)) or []
    updated_history = [entry] + history[:11]
    redis_set_json(client, _history_key(kind), updated_history, CHECK_HISTORY_TTL_SECONDS)


def record_check_result(
    *,
    kind: str,
    started_at: datetime,
    success: bool,
    message: str,
    checked_items: List[Dict[str, Any]],
    error_details: Optional[str] = None,
) -> None:
    """Persist the latest check outcome while keeping the last dozen entries."""

    finished_at = datetime.utcnow()
    entry = {
        "kind": kind,
        "timestamp": started_at.isoformat() + "Z",
        "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
        "success": success,
        "message": message,
        "checked_count": len(checked_items),
        "checked_items": checked_items,
        "error": error_details,
    }

    _write_history(kind, entry)


def get_check_history(kind: str) -> List[Dict[str, Any]]:
    client = get_redis_client()
    if client is None:
        return []
    history = redis_get_json(client, _history_key(kind))
    if not isinstance(history, list):
        return []
    return history
