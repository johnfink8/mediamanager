"""Self-healing pass for pending candidates whose recommendation never landed.

A candidate enters the queue with its ``ai`` block written during ingest.
When that run fails — a gateway 502 on a schema-rejected response, a dropped
connection, an embedding outage — the item sits in the queue showing no
verdict until someone hits Retry by hand. This pass finds those items and
re-runs the annotation so transient outages heal themselves; it is registered
as the ``annotation_repairs`` APScheduler job.

Each failed repair is recorded on ``ai.repair`` (attempt count + timestamp)
and the next attempt backs off exponentially, capped at a day: an item that
is genuinely un-annotatable (TMDB can't resolve it, the gateway rejects its
output every time) then costs one run a day instead of one run an hour. A
successful repair clears the record.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

from decouple import config
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from .ai_recs import annotate_with_ai_async
from .models import IgnoreItem
from .session import db_session

logger = logging.getLogger(__name__)

# Items re-annotated per pass. Each run is a full agent conversation on the
# shared GPU; a bounded batch keeps a big backlog from starving ingest.
REPAIR_BATCH_LIMIT = max(1, int(config("AI_REPAIR_BATCH_LIMIT", default=10)))
MAX_BACKOFF_HOURS = 24.0


def backoff_seconds(attempts: int) -> float:
    """Wait after ``attempts`` failed repairs: 1h, 2h, 4h … capped."""
    return min(2.0 ** max(0, attempts - 1), MAX_BACKOFF_HOURS) * 3600.0


def _repair_state(attrs: Dict[str, Any]) -> Tuple[int, float]:
    """``(attempts, last_at)`` from ``ai.repair``; corrupt data reads as fresh."""
    ai = attrs.get("ai")
    state = ai.get("repair") if isinstance(ai, dict) else None
    if not isinstance(state, dict):
        return 0, 0.0
    try:
        return int(state.get("attempts") or 0), float(state.get("last_at") or 0.0)
    except (TypeError, ValueError):
        return 0, 0.0


async def find_broken_pending(session: AsyncSession) -> List[IgnoreItem]:
    """Pending queue items whose ``ai`` block is missing or failed.

    "Pending" matches what the user sees: shown, not added, not ignored, and
    not sitting out a defer window. Decided rows are excluded — a verdict
    nobody asked for is worse than a missing one, and the decision itself is
    the repair for those. Newest first: the freshest candidates are the ones
    the user is actually looking at.
    """
    ai = IgnoreItem.attributes.op("->")("ai")
    now = datetime.utcnow()
    stmt = (
        select(IgnoreItem)
        .where(
            IgnoreItem.shown.is_(True),
            IgnoreItem.ignore.is_(False),
            IgnoreItem.added.is_(False),
            or_(IgnoreItem.defer_until.is_(None), IgnoreItem.defer_until <= now),
            or_(ai.is_(None), ai.op("->>")("failed") == "true"),
        )
        .order_by(IgnoreItem.created_at.desc())
    )
    return list((await session.execute(stmt)).scalars())


async def repair_broken_annotations(limit: int = REPAIR_BATCH_LIMIT) -> Dict[str, int]:
    """Re-run the annotation for broken pending items. Returns run stats.

    Mirrors ``recheck_visible``: snapshot rows, run agents outside a session,
    write results back under ``AI_ANNOTATE_CONCURRENCY``. Items decided while
    an agent was running are left alone.
    """
    # Lazy import: vid_utils pulls in the indexer/arr stack; keep it off the
    # scheduler-boot path like jobs.py's other helpers do at module level.
    from .vid_utils import AI_ANNOTATE_CONCURRENCY

    now = time.time()
    async with db_session() as session:
        rows = await find_broken_pending(session)

    due: List[Dict[str, Any]] = []
    backed_off = 0
    for item in rows:
        attempts, last_at = _repair_state(item.attributes or {})
        if attempts > 0 and now - last_at < backoff_seconds(attempts):
            backed_off += 1
            continue
        if len(due) >= limit:
            break
        due.append(
            {
                "id": item.id,
                "item_type": item.item_type,
                "uid": item.uid,
                "title": item.title,
                "attrs": dict(item.attributes or {}),
                "attempts": attempts,
            }
        )

    stats = {
        "found": len(rows),
        "backed_off": backed_off,
        "attempted": len(due),
        "repaired": 0,
        "still_failed": 0,
        "decided_away": 0,
    }
    if not due:
        return stats

    logger.info(
        "Annotation repair: %d broken pending, %d backed off, %d to retry",
        len(rows),
        backed_off,
        len(due),
    )

    semaphore = asyncio.Semaphore(AI_ANNOTATE_CONCURRENCY)

    async def _annotate(c: Dict[str, Any]) -> Any:
        attrs = dict(c["attrs"])
        attrs.pop("_synopsis_vector_tmp", None)
        try:
            async with semaphore:
                return await annotate_with_ai_async(
                    c["item_type"], c["uid"], c["title"], attrs
                )
        except Exception as exc:
            # annotate_with_ai_async turns its own failures into a failed ai
            # block; anything escaping here is unexpected (e.g. the DB dying
            # mid-payload). Record it like one so the backoff still applies.
            logger.exception(
                "repair annotation crashed for %s:%s", c["item_type"], c["uid"]
            )
            return exc

    results = await asyncio.gather(*(_annotate(c) for c in due))

    async with db_session() as session:
        for c, outcome in zip(due, results):
            fresh = await session.get(IgnoreItem, c["id"])
            if fresh is None or fresh.ignore or fresh.added or not fresh.shown:
                # The user decided (or un-shelved) while the agent ran —
                # their state wins, never write a stale verdict over it.
                stats["decided_away"] += 1
                continue
            if isinstance(outcome, BaseException):
                attrs = dict(c["attrs"])
                ai = dict(attrs.get("ai") or {})
                ai.update(
                    {
                        "failed": True,
                        "failure": {
                            "code": outcome.__class__.__name__,
                            "message": str(outcome),
                            "stage": "repair",
                        },
                    }
                )
                attrs["ai"] = ai
            else:
                attrs = outcome
                ai = attrs.get("ai") or {}
            if not ai.get("failed"):
                ai.pop("repair", None)
                stats["repaired"] += 1
                logger.info(
                    "repaired %s:%s '%s' (attempt %d)",
                    c["item_type"],
                    c["uid"],
                    c["title"],
                    c["attempts"] + 1,
                )
            else:
                ai["repair"] = {"attempts": c["attempts"] + 1, "last_at": now}
                stats["still_failed"] += 1
                logger.warning(
                    "repair %s:%s '%s' failed again (attempt %d): %s",
                    c["item_type"],
                    c["uid"],
                    c["title"],
                    c["attempts"] + 1,
                    ai.get("failure"),
                )
            fresh.attributes = attrs
            flag_modified(fresh, "attributes")
            session.add(fresh)
        await session.commit()
    return stats
