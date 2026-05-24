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
from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import IgnoreItem
from .session import db_session

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = config("OPENAI_EMBEDDING_MODEL", default="text-embedding-3-small")

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
    """Concatenate the fields Weaviate's text2vec-openai used."""
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
    k: int = 20,
    n_show: int = 6,
) -> Optional[Dict[str, Any]]:
    """Of the ``k`` titles most similar to the candidate by synopsis, how many
    the user added.

    The strongest taste signal we have: it measures whether the user actually
    keeps things like *this* candidate, independent of how the genre is
    labelled. Returns ``added_of_top`` (count added among the nearest ``k``)
    plus the closest ``n_show`` titles with their added flag, or ``None`` when
    the corpus has nothing to compare against.
    """
    dist = IgnoreItem.synopsis_vector.cosine_distance(candidate_vec).label("d")
    rows = (
        await session.execute(
            select(IgnoreItem.title, IgnoreItem.added, dist)
            .where(
                IgnoreItem.item_type == item_type,
                IgnoreItem.synopsis_vector.is_not(None),
                IgnoreItem.uid != uid,
            )
            .order_by(dist)
            .limit(k)
        )
    ).all()
    if not rows:
        return None
    return {
        "added_of_top": sum(1 for _, added, _ in rows if added),
        "k": len(rows),
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
