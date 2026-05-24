"""Lookup tools that fan out to Plex / Radarr / Sonarr / DB.

- ``get_item_details`` — single-item deep dive (DB + Plex for movies).
- ``get_user_history`` — recent Plex plays + recommendation feedback rows.
- ``check_added_history`` — for previously-added items, report download
  state (Radarr/Sonarr) and Plex view counts, gated on release date so
  unreleased items don't look like dropped balls.

The Plex/Radarr/Sonarr async wrappers are imported at module scope so the
test harness can patch them with ``patch.object(inspections, ...)``.
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from agents import RunContextWrapper
from sqlalchemy import select

from ..models import IgnoreItem, MovieRecommendationRecord
from ..plex_utils import aget_plex_details, aget_recently_played
from ..radarr_utils import aget_movie
from ..session import db_session
from ..sonarr_utils import aget_series
from .base import ToolContext
from .safe_tool import safe_tool
from .shared import (
    CAST_LIMIT,
    PLEX_SUMMARY_CLIP,
    REASON_CLIP,
    SYNOPSIS_CLIP,
    attrs_get_genres,
    clip,
    clip_list_of_strings,
    decision,
    enforce_result_budget,
)

logger = logging.getLogger(__name__)


@safe_tool
async def get_item_details(
    wrapper: RunContextWrapper[ToolContext],
    uid: str,
) -> Dict[str, Any]:
    """Look up full details for one item by uid: synopsis, cast, ratings, language.

    For movies, also returns user-watch metrics (view_count, last_viewed_at,
    audience_rating, user_rating) and plex_status. plex_status values:
    'in_library' (currently in Plex; check view_count for real engagement —
    high count = strong like), 'missing_from_library' (added in DB but no
    longer in Plex; likely deleted — strong negative signal),
    'not_in_library' (never added; absence carries no signal), 'unknown'
    (not queried, e.g. shows). Use after a search tool surfaces a uid you
    want to dig into.

    Args:
        uid: IMDB id (movies, e.g. 'tt0111161') or TVDB id (shows). Use uids
            returned by other search tools.
    """
    ctx = wrapper.context
    uid = (uid or "").strip()
    if not uid:
        return {"error": "uid is required"}

    async with db_session() as session:
        result = await session.execute(
            select(IgnoreItem)
            .where(IgnoreItem.item_type == ctx.item_type, IgnoreItem.uid == uid)
            .limit(1)
        )
        row = result.scalars().first()
        if row is None:
            return {"error": f"no {ctx.item_type} item with uid {uid}"}
        attrs = row.attributes or {}
        ai = attrs.get("ai") or {}
        added_flag = bool(row.added)
        title_for_plex = row.title
        details: Dict[str, Any] = {
            "uid": row.uid,
            "title": row.title,
            "decision": decision(row),
            "genres": attrs_get_genres(attrs),
            "year": attrs.get("year"),
            "cast": clip_list_of_strings(attrs.get("cast"), CAST_LIMIT),
            "network": attrs.get("network"),
            "studio": attrs.get("studio"),
            "director": attrs.get("director"),
            "runtime": attrs.get("runtime"),
            "language": attrs.get("originalLanguage"),
            "synopsis": clip(ai.get("synopsis"), SYNOPSIS_CLIP),
            "view_count": None,
            "last_viewed_at": None,
            "audience_rating": None,
            "user_rating": None,
            "plex_status": "unknown",
        }
        # Per-source ratings — only include populated ones (kept aligned
        # with shared.summarize_item's output shape).
        if attrs.get("rating_value") is not None:
            details["rating"] = attrs.get("rating_value")
            if attrs.get("rating_votes") is not None:
                details["rating_votes"] = attrs.get("rating_votes")
        for label, value_key, votes_key in [
            ("imdb", "imdbuser_value", "imdbuser_votes"),
            ("tmdb", "tmdbuser_value", "tmdbuser_votes"),
            ("trakt", "traktuser_value", "traktuser_votes"),
        ]:
            val = attrs.get(value_key)
            if val is not None:
                details[f"{label}_rating"] = val
                v = attrs.get(votes_key)
                if v is not None:
                    details[f"{label}_votes"] = v
        for label, value_key in [
            ("rt", "rottenTomatoesuser_value"),
            ("metacritic", "metacriticuser_value"),
        ]:
            val = attrs.get(value_key)
            if val is not None:
                details[label] = val

    if ctx.item_type == "mv" and title_for_plex:
        year = attrs.get("year")
        try:
            year_int = int(year) if year is not None else None
        except (TypeError, ValueError):
            year_int = None
        plex = await aget_plex_details(title_for_plex, year_int)
        if plex:
            details["view_count"] = plex.get("viewCount", 0)
            details["last_viewed_at"] = plex.get("lastViewedAt")
            details["audience_rating"] = plex.get("audienceRating")
            details["user_rating"] = plex.get("userRating")
            details["plex_status"] = "in_library"
            extras = {
                k: v
                for k, v in plex.items()
                if k
                not in {"viewCount", "lastViewedAt", "audienceRating", "userRating"}
            }
            if "summary" in extras:
                extras["summary"] = clip(extras["summary"], PLEX_SUMMARY_CLIP)
            if extras:
                details["plex_extras"] = extras
        else:
            details["plex_status"] = (
                "missing_from_library" if added_flag else "not_in_library"
            )

    return enforce_result_budget(details, "get_item_details", ctx.candidate.get("uid"))


@safe_tool
async def get_user_history(
    wrapper: RunContextWrapper[ToolContext],
    limit: int = 15,
) -> Dict[str, Any]:
    """Recent user activity: items recently watched and prior recommendation feedback.

    Returns recently-played Plex entries and recommendation feedback rows
    (LIKE / NOT_NOW / NEVER). Use to calibrate against current taste rather
    than stale catalog state.

    Args:
        limit: Max entries per source. Default 15.
    """
    ctx = wrapper.context
    limit = max(1, min(int(limit or 15), 50))

    plex_history: List[Dict[str, Any]] = []
    try:
        plays = await aget_recently_played(limit)
        for entry in plays[:limit]:
            plex_history.append(
                {
                    "title": entry.get("title"),
                    "type": entry.get("type"),
                    "viewed_at": entry.get("viewedAt"),
                    "year": entry.get("year"),
                }
            )
    except Exception:
        logger.exception("get_user_history: plex fetch failed")

    rec_history: List[Dict[str, Any]] = []
    try:
        records = await MovieRecommendationRecord.recent_history(limit)
        for r in list(records)[:limit]:
            rec_history.append(
                {
                    "title": r.recommended_title,
                    "uid": r.recommended_imdb_id,
                    "preference": r.preference.value if r.preference else None,
                    "reason": clip(r.recommended_reason, REASON_CLIP),
                }
            )
    except Exception:
        logger.exception("get_user_history: recommendation history fetch failed")

    return enforce_result_budget(
        {"recently_watched": plex_history, "recent_recommendations": rec_history},
        "get_user_history",
        ctx.candidate.get("uid"),
    )


def _plex_view_count(attrs: Dict[str, Any]) -> Optional[int]:
    raw = attrs.get("viewCount")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    return None


def _iso_from_unix(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _parse_iso_date(value: Any) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _movie_release_date(movie: Dict[str, Any]) -> Optional[str]:
    for key in ("digitalRelease", "physicalRelease", "inCinemas"):
        raw = movie.get(key)
        if not raw:
            continue
        dt = _parse_iso_date(raw)
        if dt is not None:
            return dt.date().isoformat()
    return None


def _movie_release_status(movie: Dict[str, Any]) -> str:
    status = str(movie.get("status") or "").lower()
    if status == "released":
        return "released"
    rel = _movie_release_date(movie)
    if rel:
        try:
            rel_dt = datetime.fromisoformat(rel).replace(tzinfo=timezone.utc)
            if rel_dt <= datetime.now(tz=timezone.utc):
                return "released"
            return "unreleased"
        except ValueError:
            pass
    if status in ("announced", "incinemas", "tba"):
        return "unreleased"
    return "unknown"


async def _movie_followup(item: IgnoreItem) -> Dict[str, Any]:
    attrs = item.attributes or {}
    movie = await aget_movie(item.uid)
    out: Dict[str, Any] = {
        "uid": item.uid,
        "title": item.title,
        "item_type": "mv",
        "added_at": _iso_from_unix(item.created_at),
        "download_status": "unknown",
        "release_status": "unknown",
        "release_date": None,
        "view_count": _plex_view_count(attrs),
        "last_viewed_at": attrs.get("lastViewedAt"),
    }
    if isinstance(movie, dict):
        out["download_status"] = "downloaded" if movie.get("hasFile") else "missing"
        out["release_status"] = _movie_release_status(movie)
        out["release_date"] = _movie_release_date(movie)
    else:
        out["download_status"] = "not_tracked"
    return out


async def _series_followup(item: IgnoreItem) -> Dict[str, Any]:
    attrs = item.attributes or {}
    out: Dict[str, Any] = {
        "uid": item.uid,
        "title": item.title,
        "item_type": "tv",
        "added_at": _iso_from_unix(item.created_at),
        "download_status": "unknown",
        "release_status": "unknown",
        "release_date": None,
        "episode_file_count": None,
        "episode_count": None,
        "view_count": _plex_view_count(attrs),
        "last_viewed_at": attrs.get("lastViewedAt"),
    }
    try:
        tvdb_int = int(item.uid)
    except (TypeError, ValueError):
        out["download_status"] = "not_tracked"
        return out

    try:
        series = await aget_series(tvdb_int)
    except Exception:
        logger.exception("check_added_history: sonarr fetch failed for %s", item.uid)
        return out

    if not isinstance(series, dict):
        out["download_status"] = "not_tracked"
        return out

    stats_raw = series.get("statistics")
    stats: Dict[str, Any] = stats_raw if isinstance(stats_raw, dict) else {}
    file_count = stats.get("episodeFileCount")
    ep_count = stats.get("episodeCount")
    out["episode_file_count"] = file_count
    out["episode_count"] = ep_count
    if isinstance(file_count, int) and isinstance(ep_count, int) and ep_count > 0:
        if file_count == 0:
            out["download_status"] = "missing"
        elif file_count >= ep_count:
            out["download_status"] = "downloaded"
        else:
            out["download_status"] = "partial"
    elif isinstance(file_count, int):
        out["download_status"] = "downloaded" if file_count > 0 else "missing"

    first_aired = _parse_iso_date(series.get("firstAired"))
    if first_aired is not None:
        out["release_date"] = first_aired.date().isoformat()
        if first_aired <= datetime.now(tz=timezone.utc):
            out["release_status"] = "released"
        else:
            out["release_status"] = "unreleased"
    else:
        status = str(series.get("status") or "").lower()
        if status in ("continuing", "ended"):
            out["release_status"] = "released"
        elif status == "upcoming":
            out["release_status"] = "unreleased"
    return out


def _empty_followup_summary() -> Dict[str, int]:
    return {
        "total": 0,
        "downloaded": 0,
        "missing_or_partial": 0,
        "unreleased": 0,
        "watched_movies": 0,
    }


@safe_tool
async def check_added_history(
    wrapper: RunContextWrapper[ToolContext],
    limit: int = 15,
    days_back: Optional[int] = None,
    item_type: Optional[Literal["mv", "tv", "any"]] = None,
) -> Dict[str, Any]:
    """Review past recommendations the user accepted, and how each one panned out.

    Use this to calibrate against your own track record: items you suggested,
    the user added, and then either watched, ignored, or are still waiting on.

    download_status: 'downloaded' (available to watch), 'missing' (not yet
    available), 'partial' (some episodes available, TV only), 'not_tracked'
    (no longer being tracked), 'unknown'. release_status: 'released',
    'unreleased' (accepted before public availability — a 'missing' download
    here is NOT a negative signal), 'unknown'.

    view_count > 0: watched to completion at least once — strong positive
    signal. view_count == 0 with last_viewed_at null on a released,
    downloaded item: in the library but never opened — negative signal
    (especially if the item has been available a while). view_count == 0
    with last_viewed_at set: started but didn't finish — mild negative, the
    user dropped out. view_count is null: not in the library / not yet
    scanned — no signal either way.

    Defaults to the candidate's item_type; pass item_type='any' to span both.

    Args:
        limit: Max items to return, 1-50. Default 15.
        days_back: Only include items added within the last N days. Optional.
        item_type: Filter by media type. Defaults to the candidate's type;
            pass 'any' to span both.
    """
    ctx = wrapper.context
    limit = max(1, min(int(limit or 15), 50))
    days_back = int(days_back) if days_back is not None else None
    requested_type = item_type or ctx.item_type
    if requested_type not in ("mv", "tv", "any"):
        requested_type = ctx.item_type

    cutoff_ts: Optional[int] = None
    if days_back is not None:
        cutoff_ts = int(datetime.now(tz=timezone.utc).timestamp()) - days_back * 86400

    async with db_session() as session:
        stmt = select(IgnoreItem).where(
            IgnoreItem.added.is_(True),
            IgnoreItem.created_at.isnot(None),
        )
        if requested_type != "any":
            stmt = stmt.where(IgnoreItem.item_type == requested_type)
        if cutoff_ts is not None:
            stmt = stmt.where(IgnoreItem.created_at >= cutoff_ts)
        stmt = stmt.order_by(IgnoreItem.created_at.desc()).limit(limit)
        result = await session.execute(stmt)
        snapshots: List[IgnoreItem] = list(result.scalars())
        for r in snapshots:
            session.expunge(r)

    if not snapshots:
        return {"results": [], "summary": _empty_followup_summary()}

    tasks = []
    for item in snapshots:
        if item.item_type == "mv":
            tasks.append(_movie_followup(item))
        else:
            tasks.append(_series_followup(item))
    results = await asyncio.gather(*tasks, return_exceptions=True)

    rendered: List[Dict[str, Any]] = []
    summary = _empty_followup_summary()
    for entry in results:
        if isinstance(entry, BaseException):
            logger.exception("check_added_history: per-item lookup failed: %s", entry)
            continue
        rendered.append(entry)
        summary["total"] += 1
        if entry["download_status"] == "downloaded":
            summary["downloaded"] += 1
        elif entry["download_status"] in ("missing", "partial"):
            summary["missing_or_partial"] += 1
        if entry["release_status"] == "unreleased":
            summary["unreleased"] += 1
        if entry.get("view_count"):
            summary["watched_movies"] += 1

    return enforce_result_budget(
        {"results": rendered, "summary": summary},
        "check_added_history",
        ctx.candidate.get("uid"),
    )
