"""Search tools: synopsis (Weaviate), genre (DB), network/studio (DB).

All three return the same ``{results, decision_counts}`` shape so the agent
sees one vocabulary regardless of where the data came from.
"""

from typing import Any, Dict, List, Optional

from agents import RunContextWrapper

from ..models import IgnoreItem
from ..session import db_session
from ..weaviate_client import asearch_by_synopsis
from .base import ToolContext
from .safe_tool import safe_tool
from .shared import (
    attrs_get_genres,
    decision,
    empty_decision_counts,
    enforce_result_budget,
    query_db_items,
    row_passes_filters,
    summarize_item,
)


def _filter_dict(
    language: Optional[str],
    director: Optional[str],
    runtime_min: Optional[int],
    runtime_max: Optional[int],
    rating_min: Optional[float],
    votes_min: Optional[int],
    year_min: Optional[int],
    year_max: Optional[int],
) -> Dict[str, Any]:
    """Repack named filter args into the shape ``row_passes_filters`` expects."""
    return {
        k: v
        for k, v in {
            "language": language,
            "director": director,
            "runtime_min": runtime_min,
            "runtime_max": runtime_max,
            "rating_min": rating_min,
            "votes_min": votes_min,
            "year_min": year_min,
            "year_max": year_max,
        }.items()
        if v is not None and v != ""
    }


@safe_tool
async def search_similar_by_synopsis(
    wrapper: RunContextWrapper[ToolContext],
    query: str,
    limit: int = 10,
    language: Optional[str] = None,
    director: Optional[str] = None,
    runtime_min: Optional[int] = None,
    runtime_max: Optional[int] = None,
    rating_min: Optional[float] = None,
    votes_min: Optional[int] = None,
    year_min: Optional[int] = None,
    year_max: Optional[int] = None,
) -> Dict[str, Any]:
    """Free-text semantic search over the user's catalog.

    Use this to find items that resemble a vibe, theme, or premise (not exact
    metadata matches). Each row has a `decision` field (added | rejected |
    pending) plus a `decision_counts` summary across the result set.

    Args:
        query: Free-text description of the kind of item you want to find
            neighbours for (e.g. 'gritty cold-war espionage thriller').
        limit: Max results, 1-25. Default 10.
        language: Original language, case-insensitive substring (e.g. 'en').
        director: Director substring (movies only).
        runtime_min: Min runtime in minutes (movies: total; TV: per-episode).
        runtime_max: Max runtime in minutes.
        rating_min: Minimum rating value (0-10 scale).
        votes_min: Minimum rating-vote count.
        year_min: Earliest release year.
        year_max: Latest release year.
    """
    ctx = wrapper.context
    query = (query or "").strip()
    if not query:
        return {"error": "query is required"}
    limit = max(1, min(int(limit or 10), 25))

    raw = await asearch_by_synopsis(query, limit, ctx.item_type)
    uids = [r["uid"] for r in raw if r.get("uid")]
    db_rows = query_db_items(ctx.item_type, uids)
    filters = _filter_dict(
        language,
        director,
        runtime_min,
        runtime_max,
        rating_min,
        votes_min,
        year_min,
        year_max,
    )

    candidate_uid = ctx.candidate.get("uid")
    results: List[Dict[str, Any]] = []
    counts = empty_decision_counts()
    for hit in raw:
        uid = hit.get("uid")
        if not uid or uid == candidate_uid:
            continue
        row = db_rows.get(uid)
        if row is not None and not row_passes_filters(row, filters):
            continue
        if row is None and filters:
            continue
        item: Dict[str, Any] = {
            "uid": uid,
            "title": hit.get("title"),
            "distance": (
                round(hit["distance"], 4) if hit.get("distance") is not None else None
            ),
        }
        if row is not None:
            summary = summarize_item(row)
            counts[summary["decision"]] += 1
            for k, v in summary.items():
                if k not in ("uid", "title"):
                    item[k] = v
        results.append(item)
    return enforce_result_budget(
        {"results": results, "decision_counts": counts},
        "search_similar_by_synopsis",
        candidate_uid,
    )


@safe_tool
async def search_by_genre(
    wrapper: RunContextWrapper[ToolContext],
    genres: List[str],
    added_only: bool = False,
    limit: int = 15,
    language: Optional[str] = None,
    director: Optional[str] = None,
    runtime_min: Optional[int] = None,
    runtime_max: Optional[int] = None,
    rating_min: Optional[float] = None,
    votes_min: Optional[int] = None,
    year_min: Optional[int] = None,
    year_max: Optional[int] = None,
) -> Dict[str, Any]:
    """Find items in the user's catalog that match one or more genres.

    Use to gauge whether the user's added items overlap with the candidate's
    genres.

    Args:
        genres: Genre names. Items matching ANY genre are returned.
        added_only: If true, restrict to items the user added.
        limit: Max results, 1-50. Default 15.
        language: Original language, case-insensitive substring.
        director: Director substring (movies only).
        runtime_min: Min runtime in minutes.
        runtime_max: Max runtime in minutes.
        rating_min: Minimum rating value (0-10).
        votes_min: Minimum rating-vote count.
        year_min: Earliest release year.
        year_max: Latest release year.
    """
    ctx = wrapper.context
    if not genres:
        return {"error": "genres must be a non-empty array"}
    wanted = {str(g).strip().lower() for g in genres if str(g).strip()}
    if not wanted:
        return {"error": "no valid genres provided"}
    limit = max(1, min(int(limit or 15), 50))
    filters = _filter_dict(
        language,
        director,
        runtime_min,
        runtime_max,
        rating_min,
        votes_min,
        year_min,
        year_max,
    )

    candidate_uid = ctx.candidate.get("uid")
    matches: List[Dict[str, Any]] = []
    counts = empty_decision_counts()
    with db_session() as session:
        q = session.query(IgnoreItem).filter(IgnoreItem.item_type == ctx.item_type)
        if added_only:
            q = q.filter(IgnoreItem.added.is_(True))
        for row in q.all():
            if row.uid == candidate_uid:
                continue
            row_genres = {g.lower() for g in attrs_get_genres(row.attributes)}
            if not (row_genres & wanted):
                continue
            if not row_passes_filters(row, filters):
                continue
            counts[decision(row)] += 1
            if len(matches) < limit:
                matches.append(summarize_item(row))
    return enforce_result_budget(
        {"results": matches, "decision_counts": counts},
        "search_by_genre",
        candidate_uid,
    )


@safe_tool
async def search_by_network(
    wrapper: RunContextWrapper[ToolContext],
    network: str,
    added_only: bool = False,
    limit: int = 15,
    language: Optional[str] = None,
    director: Optional[str] = None,
    runtime_min: Optional[int] = None,
    runtime_max: Optional[int] = None,
    rating_min: Optional[float] = None,
    votes_min: Optional[int] = None,
    year_min: Optional[int] = None,
    year_max: Optional[int] = None,
) -> Dict[str, Any]:
    """Find items in the user's catalog by network (TV) or studio (movies).

    Examples: 'Apple TV+', 'HBO', 'A24'. Use this when the candidate has a
    distinctive platform — platform is often a strong positive or negative
    taste signal. Combine with added_only=true to see whether the user has
    historically liked output from that platform.

    Args:
        network: Network or studio name; case-insensitive substring match
            against network (TV) or studio (movies).
        added_only: If true, restrict to items the user added.
        limit: Max results, 1-50. Default 15.
        language: Original language substring.
        director: Director substring (movies only).
        runtime_min: Min runtime in minutes.
        runtime_max: Max runtime in minutes.
        rating_min: Minimum rating value (0-10).
        votes_min: Minimum rating-vote count.
        year_min: Earliest release year.
        year_max: Latest release year.
    """
    ctx = wrapper.context
    name_raw = (network or "").strip()
    if not name_raw:
        return {"error": "network is required"}
    needle = name_raw.lower()
    limit = max(1, min(int(limit or 15), 50))
    filters = _filter_dict(
        language,
        director,
        runtime_min,
        runtime_max,
        rating_min,
        votes_min,
        year_min,
        year_max,
    )

    candidate_uid = ctx.candidate.get("uid")
    matches: List[Dict[str, Any]] = []
    counts = empty_decision_counts()
    with db_session() as session:
        q = session.query(IgnoreItem).filter(IgnoreItem.item_type == ctx.item_type)
        if added_only:
            q = q.filter(IgnoreItem.added.is_(True))
        for row in q.all():
            if row.uid == candidate_uid:
                continue
            attrs = row.attributes or {}
            net = str(attrs.get("network") or "").lower()
            studio = str(attrs.get("studio") or "").lower()
            if needle not in net and needle not in studio:
                continue
            if not row_passes_filters(row, filters):
                continue
            counts[decision(row)] += 1
            if len(matches) < limit:
                matches.append(summarize_item(row))
    return enforce_result_budget(
        {"results": matches, "decision_counts": counts},
        "search_by_network",
        candidate_uid,
    )
