"""Search tools: synopsis (pgvector), genre (DB), network/studio (DB).

All three search *added* items only — they're for finding concrete library
examples that match a query, not for deriving taste signals. Aggregate
taste lives in the ``library_profile`` block of the user prompt; tools
return raw rows.

Rating filters are per-source (``imdb_min``, ``rt_min``, etc.) because the
sources measure different things on different scales with different
reliability profiles — see notes/rating-classifier-design.md for the full
discussion.
"""

from typing import Any, Dict, List, Optional

from agents import RunContextWrapper
from sqlalchemy import select

from ..models import IgnoreItem
from ..session import db_session
from ..vector_search import synopsis_select
from .base import ToolContext
from .safe_tool import safe_tool
from .shared import (
    attrs_get_genres,
    build_filter_clauses,
    enforce_result_budget,
    row_passes_filters,
    summarize_item,
)


def _filter_dict(
    language: Optional[str],
    director: Optional[str],
    runtime_min: Optional[int],
    runtime_max: Optional[int],
    rating_min: Optional[float],
    rating_votes_min: Optional[int],
    imdb_min: Optional[float],
    imdb_votes_min: Optional[int],
    tmdb_min: Optional[float],
    tmdb_votes_min: Optional[int],
    trakt_min: Optional[float],
    trakt_votes_min: Optional[int],
    rt_min: Optional[int],
    metacritic_min: Optional[int],
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
            "rating_votes_min": rating_votes_min,
            "imdb_min": imdb_min,
            "imdb_votes_min": imdb_votes_min,
            "tmdb_min": tmdb_min,
            "tmdb_votes_min": tmdb_votes_min,
            "trakt_min": trakt_min,
            "trakt_votes_min": trakt_votes_min,
            "rt_min": rt_min,
            "metacritic_min": metacritic_min,
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
    rating_votes_min: Optional[int] = None,
    imdb_min: Optional[float] = None,
    imdb_votes_min: Optional[int] = None,
    tmdb_min: Optional[float] = None,
    tmdb_votes_min: Optional[int] = None,
    trakt_min: Optional[float] = None,
    trakt_votes_min: Optional[int] = None,
    rt_min: Optional[int] = None,
    metacritic_min: Optional[int] = None,
    year_min: Optional[int] = None,
    year_max: Optional[int] = None,
) -> Dict[str, Any]:
    """Free-text semantic search over the user's added library.

    Returns added items whose synopsis is semantically close to ``query``.
    Use when you want concrete examples of accepted titles in a vibe/theme
    that resembles the candidate (not for aggregate taste — that lives in
    ``library_profile``).

    Args:
        query: Free-text description of the kind of item you want to find
            neighbours for (e.g. 'gritty cold-war espionage thriller').
        limit: Max results, 1-25. Default 10.
        language: Original language, case-insensitive substring (e.g. 'en').
        director: Director substring (movies only).
        runtime_min: Min runtime in minutes (movies: total; TV: per-episode).
        runtime_max: Max runtime in minutes.
        rating_min: Minimum generic indexer-aggregate rating (0-10). Populated
            for TV via Sonarr; 0% coverage on movies (use the source-specific
            filters there).
        rating_votes_min: Minimum companion vote count for ``rating_min``.
        imdb_min: Minimum IMDB user rating (0-10).
        imdb_votes_min: Minimum IMDB user vote count. Pair with ``imdb_min``
            to suppress small-sample noise (IMDB at <500 votes is unreliable).
        tmdb_min: Minimum TMDB user rating (0-10).
        tmdb_votes_min: Minimum TMDB vote count.
        trakt_min: Minimum Trakt user rating (0-10).
        trakt_votes_min: Minimum Trakt vote count.
        rt_min: Minimum Rotten Tomatoes Tomatometer score (0-100, critic
            aggregate — no companion vote count).
        metacritic_min: Minimum Metacritic score (0-100, critic aggregate —
            no companion vote count).
        year_min: Earliest release year.
        year_max: Latest release year.
    """
    ctx = wrapper.context
    query = (query or "").strip()
    if not query:
        return {"error": "query is required"}
    limit = max(1, min(int(limit or 10), 25))

    candidate_uid = ctx.candidate.get("uid")
    filters = _filter_dict(
        language,
        director,
        runtime_min,
        runtime_max,
        rating_min,
        rating_votes_min,
        imdb_min,
        imdb_votes_min,
        tmdb_min,
        tmdb_votes_min,
        trakt_min,
        trakt_votes_min,
        rt_min,
        metacritic_min,
        year_min,
        year_max,
    )

    stmt = await synopsis_select(query, ctx.item_type)
    if stmt is None:
        return {"results": []}
    stmt = stmt.where(IgnoreItem.added.is_(True))
    if candidate_uid:
        stmt = stmt.where(IgnoreItem.uid != candidate_uid)
    for clause in build_filter_clauses(filters):
        stmt = stmt.where(clause)
    stmt = stmt.limit(limit)

    async with db_session() as session:
        raw = (await session.execute(stmt)).all()

    results: List[Dict[str, Any]] = []
    for row, distance in raw:
        summary = summarize_item(row)
        item: Dict[str, Any] = {
            "uid": row.uid,
            "title": row.title,
            "distance": round(float(distance), 4),
        }
        for k, v in summary.items():
            if k not in ("uid", "title", "decision"):
                # ``decision`` is always "added" by construction — strip it
                # to avoid burning tokens on a constant.
                item[k] = v
        results.append(item)
    return enforce_result_budget(
        {"results": results},
        "search_similar_by_synopsis",
        candidate_uid,
    )


@safe_tool
async def search_by_genre(
    wrapper: RunContextWrapper[ToolContext],
    genres: List[str],
    limit: int = 15,
    language: Optional[str] = None,
    director: Optional[str] = None,
    runtime_min: Optional[int] = None,
    runtime_max: Optional[int] = None,
    rating_min: Optional[float] = None,
    rating_votes_min: Optional[int] = None,
    imdb_min: Optional[float] = None,
    imdb_votes_min: Optional[int] = None,
    tmdb_min: Optional[float] = None,
    tmdb_votes_min: Optional[int] = None,
    trakt_min: Optional[float] = None,
    trakt_votes_min: Optional[int] = None,
    rt_min: Optional[int] = None,
    metacritic_min: Optional[int] = None,
    year_min: Optional[int] = None,
    year_max: Optional[int] = None,
) -> Dict[str, Any]:
    """Find added items in the user's library that match one or more genres.

    Use to surface concrete examples of accepted titles that share genres
    with the candidate. Aggregate genre taste (rank, share) is already in
    ``library_profile`` — don't try to derive it from these results.

    Args:
        genres: Genre names. Items matching ANY genre are returned.
        limit: Max results, 1-50. Default 15.
        language: Original language, case-insensitive substring.
        director: Director substring (movies only).
        runtime_min: Min runtime in minutes.
        runtime_max: Max runtime in minutes.
        rating_min: Minimum generic indexer-aggregate rating (0-10). Populated
            for TV via Sonarr; 0% coverage on movies (use the source-specific
            filters there).
        rating_votes_min: Minimum companion vote count for ``rating_min``.
        imdb_min: Minimum IMDB user rating (0-10).
        imdb_votes_min: Minimum IMDB user vote count.
        tmdb_min: Minimum TMDB user rating (0-10).
        tmdb_votes_min: Minimum TMDB vote count.
        trakt_min: Minimum Trakt user rating (0-10).
        trakt_votes_min: Minimum Trakt vote count.
        rt_min: Minimum Rotten Tomatoes Tomatometer score (0-100, critic
            aggregate).
        metacritic_min: Minimum Metacritic score (0-100, critic aggregate).
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
        rating_votes_min,
        imdb_min,
        imdb_votes_min,
        tmdb_min,
        tmdb_votes_min,
        trakt_min,
        trakt_votes_min,
        rt_min,
        metacritic_min,
        year_min,
        year_max,
    )

    candidate_uid = ctx.candidate.get("uid")
    matches: List[Dict[str, Any]] = []
    async with db_session() as session:
        result = await session.execute(
            select(IgnoreItem).where(
                IgnoreItem.item_type == ctx.item_type,
                IgnoreItem.added.is_(True),
            )
        )
        for row in result.scalars():
            if row.uid == candidate_uid:
                continue
            row_genres = {g.lower() for g in attrs_get_genres(row.attributes)}
            if not (row_genres & wanted):
                continue
            if not row_passes_filters(row, filters):
                continue
            summary = summarize_item(row)
            summary.pop("decision", None)
            if len(matches) < limit:
                matches.append(summary)
            else:
                break
    return enforce_result_budget(
        {"results": matches},
        "search_by_genre",
        candidate_uid,
    )


@safe_tool
async def search_by_network(
    wrapper: RunContextWrapper[ToolContext],
    network: str,
    limit: int = 15,
    language: Optional[str] = None,
    director: Optional[str] = None,
    runtime_min: Optional[int] = None,
    runtime_max: Optional[int] = None,
    rating_min: Optional[float] = None,
    rating_votes_min: Optional[int] = None,
    imdb_min: Optional[float] = None,
    imdb_votes_min: Optional[int] = None,
    tmdb_min: Optional[float] = None,
    tmdb_votes_min: Optional[int] = None,
    trakt_min: Optional[float] = None,
    trakt_votes_min: Optional[int] = None,
    rt_min: Optional[int] = None,
    metacritic_min: Optional[int] = None,
    year_min: Optional[int] = None,
    year_max: Optional[int] = None,
) -> Dict[str, Any]:
    """Find added items in the user's library by network (TV) or studio (movies).

    Use to surface concrete examples of accepted titles from a specific
    platform. Aggregate studio/network taste (rank, share) is already in
    ``library_profile`` — don't try to derive it from these results.

    Args:
        network: Network or studio name; case-insensitive substring match
            against network (TV) or studio (movies).
        limit: Max results, 1-50. Default 15.
        language: Original language substring.
        director: Director substring (movies only).
        runtime_min: Min runtime in minutes.
        runtime_max: Max runtime in minutes.
        rating_min: Minimum generic indexer-aggregate rating (0-10). Populated
            for TV via Sonarr; 0% coverage on movies (use the source-specific
            filters there).
        rating_votes_min: Minimum companion vote count for ``rating_min``.
        imdb_min: Minimum IMDB user rating (0-10).
        imdb_votes_min: Minimum IMDB user vote count.
        tmdb_min: Minimum TMDB user rating (0-10).
        tmdb_votes_min: Minimum TMDB vote count.
        trakt_min: Minimum Trakt user rating (0-10).
        trakt_votes_min: Minimum Trakt vote count.
        rt_min: Minimum Rotten Tomatoes Tomatometer score (0-100, critic
            aggregate).
        metacritic_min: Minimum Metacritic score (0-100, critic aggregate).
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
        rating_votes_min,
        imdb_min,
        imdb_votes_min,
        tmdb_min,
        tmdb_votes_min,
        trakt_min,
        trakt_votes_min,
        rt_min,
        metacritic_min,
        year_min,
        year_max,
    )

    candidate_uid = ctx.candidate.get("uid")
    matches: List[Dict[str, Any]] = []
    async with db_session() as session:
        result = await session.execute(
            select(IgnoreItem).where(
                IgnoreItem.item_type == ctx.item_type,
                IgnoreItem.added.is_(True),
            )
        )
        for row in result.scalars():
            if row.uid == candidate_uid:
                continue
            attrs = row.attributes or {}
            net = str(attrs.get("network") or "").lower()
            studio = str(attrs.get("studio") or "").lower()
            if needle not in net and needle not in studio:
                continue
            if not row_passes_filters(row, filters):
                continue
            summary = summarize_item(row)
            summary.pop("decision", None)
            if len(matches) < limit:
                matches.append(summary)
            else:
                break
    return enforce_result_budget(
        {"results": matches},
        "search_by_network",
        candidate_uid,
    )
