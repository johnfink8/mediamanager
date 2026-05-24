"""Embedding + synopsis-vector query construction for the postgres+pgvector layer.

Two responsibilities:

* ``upsert_item_vector`` — embed ``title + synopsis`` and stash the vector
  on the matching ``IgnoreItem`` row.
* ``synopsis_select`` — build an unbounded ``SELECT (IgnoreItem, distance)
  ORDER BY synopsis_vector <=> :q``. The caller layers ``.where(...)``
  clauses and a ``.limit(k)`` and executes once. There's no specialised
  search helper because the search *is* the SELECT with an ORDER BY.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from decouple import config
from openai import AsyncOpenAI
from sqlalchemy import Integer, Select, String, and_, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import IgnoreItem
from .session import db_session

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = config("OPENAI_EMBEDDING_MODEL", default="text-embedding-3-small")

# Neighbours are constrained to the candidate's release window so the count
# means the same thing regardless of how recent the candidate is. Inbound
# candidates are always current releases, which sit in a mostly-passed-on
# neighbourhood; comparing them against same-era titles (rather than the
# decades-old library) keeps the signal era-consistent.
NEIGHBOR_YEAR_WINDOW = 2
_YEAR_RE = r"^[0-9]+$"


def _year_between(lo: int, hi: int) -> Any:
    """``attributes->>'year'`` cast to int and bounded to ``[lo, hi]``.

    Guarded by a regex so non-numeric stored years drop out instead of
    raising a postgres cast error (mirrors ``ai_tools.shared._numeric_clause``).
    """
    expr = cast(IgnoreItem.attributes.op("->>")("year"), String)
    return and_(expr.op("~")(_YEAR_RE), cast(expr, Integer).between(lo, hi))


_openai_client: Optional[AsyncOpenAI] = None


def _get_openai_client() -> Optional[AsyncOpenAI]:
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    try:
        _openai_client = AsyncOpenAI(api_key=config("OPENAI_API_KEY"))
        return _openai_client
    except Exception:
        logger.exception("Failed to initialize OpenAI client for embeddings")
        return None


async def _embed(text: str) -> Optional[List[float]]:
    client = _get_openai_client()
    if client is None:
        return None
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    resp = await client.embeddings.create(model=EMBEDDING_MODEL, input=cleaned)
    return list(resp.data[0].embedding)


def _embedding_source(title: str, synopsis: Optional[str]) -> str:
    """Concatenate the fields that get embedded: title + synopsis."""
    parts = [p for p in (title, synopsis) if p]
    return " ".join(parts)


async def upsert_item_vector(
    attrs: Dict[str, Any],
    item_type: str,
    uid: str,
    title: str,
    synopsis: Optional[str],
) -> Dict[str, Any]:
    """Embed the candidate and persist the vector on the row.

    If a matching ``IgnoreItem`` row already exists, the vector is written
    directly via UPDATE. If no row matches yet — the new-candidate ingest
    path annotates *before* ``IgnoreItem.create``, see
    ``vid_utils._arun_movie_candidates`` — the vector is stashed into
    ``attrs["_synopsis_vector_tmp"]`` so the caller can attach it after
    insert. Silently no-ops if there's nothing to embed or OpenAI is
    unavailable; annotation should not fail just because the vector
    couldn't be written.
    """
    text_to_embed = _embedding_source(title, synopsis)
    if not text_to_embed:
        return attrs
    try:
        vec = await _embed(text_to_embed)
    except Exception:
        logger.exception("Embedding failed for %s:%s", item_type, uid)
        return attrs
    if vec is None:
        return attrs
    async with db_session() as session:
        result = await session.execute(
            update(IgnoreItem)
            .where(IgnoreItem.uid == uid)
            .where(IgnoreItem.item_type == item_type)
            .values(synopsis_vector=vec)
        )
        await session.commit()
    if result.rowcount == 0:
        # Row doesn't exist yet — hand the vector off to the caller for
        # attach-after-create. ``vid_utils`` looks for this key and pops it
        # onto the row after ``IgnoreItem.create``.
        attrs["_synopsis_vector_tmp"] = vec
    return attrs


async def synopsis_neighbor_summary(
    session: AsyncSession,
    item_type: str,
    uid: str,
    candidate_vec: List[float],
    candidate_year: Optional[int] = None,
    k: int = 20,
    n_show: int = 6,
    year_window: int = NEIGHBOR_YEAR_WINDOW,
) -> Optional[Dict[str, Any]]:
    """Of the ``k`` *decided* titles most similar to the candidate by synopsis,
    how many the user kept — read against the user's keep rate for that era.

    The strongest taste signal we have: it measures whether the user actually
    keeps things like *this* candidate, independent of how the genre is
    labelled. Two constraints make the raw count honest:

    * **decided only** (``ignore=True``) — a still-in-queue candidate is
      neither kept nor passed, so it must not count as a negative. Keeps the
      signal stable if the review queue ever backs up.
    * **same release era** (``candidate_year ± year_window``) — inbound
      candidates are always current releases and live in a mostly-passed-on
      neighbourhood; bounding to the candidate's window stops a decades-old,
      heavily-kept library from inflating or distorting the count.

    Returns ``added_of_top`` (kept among the nearest ``k``), ``k``,
    ``base_rate`` (the user's keep rate across the *same* decided + same-era
    pool — so the consumer reads ``added_of_top`` on the curve, not as an
    absolute), the ``era`` window, and the closest ``n_show`` titles. ``None``
    when the corpus has nothing to compare against. Falls back to an
    era-unbounded pool when ``candidate_year`` is missing.
    """
    where = [
        IgnoreItem.item_type == item_type,
        IgnoreItem.synopsis_vector.is_not(None),
        IgnoreItem.uid != uid,
        IgnoreItem.ignore.is_(True),
    ]
    era: Optional[List[int]] = None
    if candidate_year is not None:
        lo, hi = candidate_year - year_window, candidate_year + year_window
        where.append(_year_between(lo, hi))
        era = [lo, hi]

    dist = IgnoreItem.synopsis_vector.cosine_distance(candidate_vec).label("d")
    title = func.coalesce(IgnoreItem.checked_title, IgnoreItem.title).label("title")
    rows = (
        await session.execute(
            select(title, IgnoreItem.added, dist).where(*where).order_by(dist).limit(k)
        )
    ).all()
    if not rows:
        return None

    # Same pool, no limit: the user's typical keep rate for this era. The
    # consumer compares added_of_top against this rather than reading a low
    # absolute count as a veto.
    totals = (
        await session.execute(
            select(
                func.count().label("n"),
                func.count().filter(IgnoreItem.added.is_(True)).label("kept"),
            ).where(*where)
        )
    ).one()
    base_rate = totals.kept / totals.n if totals.n else None

    return {
        "added_of_top": sum(1 for _, added, _ in rows if added),
        "k": len(rows),
        "base_rate": round(base_rate, 3) if base_rate is not None else None,
        "era": era,
        "nearest": [
            {"title": title, "added": bool(added), "distance": round(float(d), 3)}
            for title, added, d in rows[:n_show]
        ],
    }


async def synopsis_select(query_text: str, item_type: str) -> Optional[Select]:
    """Build ``SELECT IgnoreItem, distance ORDER BY synopsis_vector <=> :q``.

    No LIMIT, no ``added`` filter, no candidate-exclusion — the caller
    layers those (and any other constraints) before executing. Returns
    ``None`` if embedding the query failed; callers should treat that as
    "no results."
    """
    try:
        vec = await _embed(query_text)
    except Exception:
        logger.exception("Embedding failed for query: %r", query_text[:120])
        return None
    if vec is None:
        return None
    distance = IgnoreItem.synopsis_vector.cosine_distance(vec)
    return (
        select(IgnoreItem, distance.label("distance"))
        .where(IgnoreItem.item_type == item_type)
        .where(IgnoreItem.synopsis_vector.is_not(None))
        .order_by(distance)
    )
