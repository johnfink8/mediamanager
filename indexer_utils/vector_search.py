"""Synopsis-vector search backed by postgres + pgvector.

Replaces the prior Weaviate-backed module. Embedding is now app-side
(OpenAI ``embeddings.create``) because pgvector has no vectorizer
module — same per-call cost as before, one less moving part.

Public surface mirrors the old ``weaviate_client``:

* ``aupsert_item_vector`` — embed ``title + synopsis`` and store the
  vector on the matching ``IgnoreItem`` row. Returns the attrs dict
  back (kept for call-site parity, no longer mutated).
* ``asearch_by_synopsis`` — embed the query, return the top-k nearest
  items of the given ``item_type`` with cosine distance.
* ``aget_nearest_neighbors`` — k-NN from an anchor item's own vector,
  used by the diagnostic ``vector_simulate_distance`` script.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from decouple import config
from openai import OpenAI
from sqlalchemy import select, update

from .models import IgnoreItem
from .session import db_session

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = config("OPENAI_EMBEDDING_MODEL", default="text-embedding-3-small")

_openai_client: Optional[OpenAI] = None


def _get_openai_client() -> Optional[OpenAI]:
    global _openai_client
    if _openai_client is not None:
        return _openai_client
    try:
        _openai_client = OpenAI(api_key=config("OPENAI_API_KEY"))
        return _openai_client
    except Exception:
        logger.exception("Failed to initialize OpenAI client for embeddings")
        return None


def _embed(text: str) -> Optional[List[float]]:
    client = _get_openai_client()
    if client is None:
        return None
    cleaned = (text or "").strip()
    if not cleaned:
        return None
    resp = client.embeddings.create(model=EMBEDDING_MODEL, input=cleaned)
    return list(resp.data[0].embedding)


def _embedding_source(title: str, synopsis: Optional[str]) -> str:
    """Concatenate the fields Weaviate's text2vec-openai used."""
    parts = [p for p in (title, synopsis) if p]
    return " ".join(parts)


def upsert_item_vector(
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
        vec = _embed(text_to_embed)
    except Exception:
        logger.exception("Embedding failed for %s:%s", item_type, uid)
        return attrs
    if vec is None:
        return attrs
    with db_session() as session:
        result = session.execute(
            update(IgnoreItem)
            .where(IgnoreItem.uid == uid)
            .where(IgnoreItem.item_type == item_type)
            .values(synopsis_vector=vec)
        )
        session.commit()
    if result.rowcount == 0:
        # Row doesn't exist yet — hand the vector off to the caller for
        # attach-after-create. ``vid_utils`` looks for this key and pops it
        # onto the row after ``IgnoreItem.create``.
        attrs["_synopsis_vector_tmp"] = vec
    return attrs


async def aupsert_item_vector(
    attrs: Dict[str, Any],
    item_type: str,
    uid: str,
    title: str,
    synopsis: Optional[str],
) -> Dict[str, Any]:
    return await asyncio.to_thread(
        upsert_item_vector, attrs, item_type, uid, title, synopsis
    )


def search_by_synopsis(
    query_text: str,
    k: int,
    item_type: str,
    *,
    added_only: bool = False,
    exclude_uid: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Return the top-k nearest items by cosine distance.

    Result shape: ``{"uid", "title", "distance"}`` (lower = closer).

    ``added_only`` and ``exclude_uid`` push the agent-tool's two universal
    filters into SQL so ``LIMIT k`` is actually the top-k of *eligible*
    items, not "top-k of everything, minus rejects" — the latter silently
    shrinks the result set whenever a rejected item lands in the K nearest.
    Default-off so diagnostic callers (e.g. ``vector_simulate_distance``)
    still see the whole index.
    """
    try:
        vec = _embed(query_text)
    except Exception:
        logger.exception("Embedding failed for query: %r", query_text[:120])
        return []
    if vec is None:
        return []
    stmt = (
        select(
            IgnoreItem.uid,
            IgnoreItem.title,
            IgnoreItem.synopsis_vector.cosine_distance(vec).label("distance"),
        )
        .where(IgnoreItem.item_type == item_type)
        .where(IgnoreItem.synopsis_vector.is_not(None))
    )
    if added_only:
        stmt = stmt.where(IgnoreItem.added.is_(True))
    if exclude_uid is not None:
        stmt = stmt.where(IgnoreItem.uid != exclude_uid)
    stmt = stmt.order_by(IgnoreItem.synopsis_vector.cosine_distance(vec)).limit(k)
    with db_session() as session:
        rows = session.execute(stmt).all()
    return [
        {"uid": uid, "title": title, "distance": float(distance)}
        for uid, title, distance in rows
    ]


async def asearch_by_synopsis(
    query_text: str,
    k: int,
    item_type: str,
    *,
    added_only: bool = False,
    exclude_uid: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(
        search_by_synopsis,
        query_text,
        k,
        item_type,
        added_only=added_only,
        exclude_uid=exclude_uid,
    )


def get_nearest_neighbors(
    uid: str, k: int, item_type: str
) -> Dict[str, Dict[str, Any]]:
    """k-NN by an anchor item's own vector. Used by the diagnostic script."""
    with db_session() as session:
        anchor = session.execute(
            select(IgnoreItem.synopsis_vector)
            .where(IgnoreItem.uid == uid)
            .where(IgnoreItem.item_type == item_type)
        ).scalar_one_or_none()
        if anchor is None:
            return {}
        rows = session.execute(
            select(
                IgnoreItem.uid,
                IgnoreItem.synopsis_vector.cosine_distance(anchor).label("distance"),
            )
            .where(IgnoreItem.item_type == item_type)
            .where(IgnoreItem.uid != uid)
            .where(IgnoreItem.synopsis_vector.is_not(None))
            .order_by(IgnoreItem.synopsis_vector.cosine_distance(anchor))
            .limit(k)
        ).all()
    return {
        neighbor_uid: {"uid": neighbor_uid, "distance": float(distance)}
        for neighbor_uid, distance in rows
    }


async def aget_nearest_neighbors(
    uid: str, k: int, item_type: str
) -> Dict[str, Dict[str, Any]]:
    return await asyncio.to_thread(get_nearest_neighbors, uid, k, item_type)
