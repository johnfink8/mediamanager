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
from typing import Any, Dict, List, Optional

from ..models import IgnoreItem, MovieRecommendationRecord
from ..plex_utils import aget_plex_details, aget_recently_played
from ..radarr_utils import aget_movie
from ..session import db_session
from ..sonarr_utils import aget_series
from .base import Tool, ToolContext, ToolResult
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


# ---------------------------------------------------------------------------
# get_item_details
# ---------------------------------------------------------------------------

GET_DETAILS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "uid": {
            "type": "string",
            "description": (
                "IMDB id (movies, e.g. 'tt0111161') or TVDB id (shows). "
                "Use uids returned by other search tools."
            ),
        },
    },
    "required": ["uid"],
}


async def _t_get_details(input_: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    uid = str(input_.get("uid") or "").strip()
    if not uid:
        return ToolResult(output={"error": "uid is required"})

    with db_session() as session:
        row = (
            session.query(IgnoreItem)
            .filter(IgnoreItem.item_type == ctx.item_type, IgnoreItem.uid == uid)
            .first()
        )
        if row is None:
            return ToolResult(
                output={"error": f"no {ctx.item_type} item with uid {uid}"}
            )
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
            "rating_value": attrs.get("rating_value"),
            "rating_votes": attrs.get("rating_votes"),
            "synopsis": clip(ai.get("synopsis"), SYNOPSIS_CLIP),
            # Plex signals — promoted to top level because view_count is one
            # of the strongest indicators of real engagement.
            "view_count": None,
            "last_viewed_at": None,
            "audience_rating": None,
            "user_rating": None,
            "plex_status": "unknown",
        }

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
            # Item not currently in Plex. If the user previously added it,
            # the most likely explanation is deletion — a strong negative
            # signal. If they never added it, absence carries no signal.
            details["plex_status"] = (
                "missing_from_library" if added_flag else "not_in_library"
            )

    return ToolResult(
        output=enforce_result_budget(
            details, "get_item_details", ctx.candidate.get("uid")
        )
    )


# ---------------------------------------------------------------------------
# get_user_history
# ---------------------------------------------------------------------------

GET_HISTORY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "description": "Max entries per source. Default 15.",
        },
    },
}


async def _t_get_history(input_: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    limit = int(input_.get("limit") or 15)
    limit = max(1, min(limit, 50))

    plex_history: List[Dict[str, Any]] = []
    try:
        # Plex's /status/sessions/history/all ignores ``maxResults`` and
        # returns the full history — slice client-side or we drown in
        # thousands of entries.
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
        records = MovieRecommendationRecord.recent_history(limit)
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

    output = {
        "recently_watched": plex_history,
        "recent_recommendations": rec_history,
    }
    return ToolResult(
        output=enforce_result_budget(
            output, "get_user_history", ctx.candidate.get("uid")
        )
    )


# ---------------------------------------------------------------------------
# check_added_history
# ---------------------------------------------------------------------------

CHECK_ADDED_HISTORY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "description": "Max items to return. Default 15.",
        },
        "days_back": {
            "type": "integer",
            "minimum": 1,
            "description": (
                "Only include items added within the last N days. "
                "Optional; default returns the most recent regardless of age."
            ),
        },
        "item_type": {
            "type": "string",
            "enum": ["mv", "tv", "any"],
            "description": (
                "Filter by media type. Default uses the candidate's type "
                "(mv or tv); pass 'any' to span both."
            ),
        },
    },
}


def _plex_view_count(attrs: Dict[str, Any]) -> Optional[int]:
    """Return Plex viewCount merged into ``attrs`` by the periodic library
    scan. ``None`` means the row hasn't been matched against Plex yet (scan
    hasn't run, or item isn't in Plex). ``0`` means matched but unwatched.
    """
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
    """Best-guess public-availability date from a Radarr movie payload."""
    for key in ("digitalRelease", "physicalRelease", "inCinemas"):
        raw = movie.get(key)
        if not raw:
            continue
        dt = _parse_iso_date(raw)
        if dt is not None:
            return dt.date().isoformat()
    return None


def _movie_release_status(movie: Dict[str, Any]) -> str:
    """released | unreleased | unknown — based on Radarr release dates/status.

    Radarr's ``status`` field (announced/inCinemas/released/deleted) is the
    primary signal; release dates back-fill it when missing.
    """
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


async def _t_check_added_history(
    input_: Dict[str, Any], ctx: ToolContext
) -> ToolResult:
    limit = int(input_.get("limit") or 15)
    limit = max(1, min(limit, 50))
    days_back_raw = input_.get("days_back")
    days_back = int(days_back_raw) if days_back_raw is not None else None
    requested_type = str(input_.get("item_type") or ctx.item_type).lower()
    if requested_type not in ("mv", "tv", "any"):
        requested_type = ctx.item_type

    cutoff_ts: Optional[int] = None
    if days_back is not None:
        cutoff_ts = int(datetime.now(tz=timezone.utc).timestamp()) - days_back * 86400

    with db_session() as session:
        q = session.query(IgnoreItem).filter(
            IgnoreItem.added.is_(True),
            IgnoreItem.created_at.isnot(None),
        )
        if requested_type != "any":
            q = q.filter(IgnoreItem.item_type == requested_type)
        if cutoff_ts is not None:
            q = q.filter(IgnoreItem.created_at >= cutoff_ts)
        rows = q.order_by(IgnoreItem.created_at.desc()).limit(limit).all()
        # Detach what we need; sessions close on block exit and the async
        # fan-out below would otherwise re-touch expired ORM objects.
        snapshots: List[IgnoreItem] = list(rows)
        for r in snapshots:
            session.expunge(r)

    if not snapshots:
        return ToolResult(output={"results": [], "summary": _empty_followup_summary()})

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

    output = {"results": rendered, "summary": summary}
    return ToolResult(
        output=enforce_result_budget(
            output, "check_added_history", ctx.candidate.get("uid")
        )
    )


GET_DETAILS_TOOL = Tool(
    name="get_item_details",
    description=(
        "Look up full details for one item by uid: synopsis, cast, "
        "ratings, language. For movies, also returns user-watch "
        "metrics (view_count, last_viewed_at, audience_rating, "
        "user_rating) and plex_status. plex_status values: "
        "'in_library' (currently in Plex; check view_count for real "
        "engagement — high count = strong like), "
        "'missing_from_library' (added in DB but no longer in Plex; "
        "likely deleted — strong negative signal), "
        "'not_in_library' (never added; absence carries no signal), "
        "'unknown' (not queried, e.g. shows). Use after a search "
        "tool surfaces a uid you want to dig into."
    ),
    input_schema=GET_DETAILS_SCHEMA,
    execute=_t_get_details,
)

GET_HISTORY_TOOL = Tool(
    name="get_user_history",
    description=(
        "Recent user activity: items recently watched and prior "
        "recommendation feedback (LIKE / NOT_NOW / NEVER). Use to "
        "calibrate against current taste rather than stale catalog state."
    ),
    input_schema=GET_HISTORY_SCHEMA,
    execute=_t_get_history,
)

CHECK_ADDED_HISTORY_TOOL = Tool(
    name="check_added_history",
    description=(
        "Review past recommendations the user accepted, and how each one "
        "panned out. Use this to calibrate against your own track record: "
        "items you suggested, the user added, and then either watched, "
        "ignored, or are still waiting on. "
        "download_status: 'downloaded' (available to watch), 'missing' "
        "(not yet available), 'partial' (some episodes available, TV "
        "only), 'not_tracked' (no longer being tracked), 'unknown'. "
        "release_status: 'released', 'unreleased' (accepted before "
        "public availability — a 'missing' download here is NOT a "
        "negative signal), 'unknown'. "
        "view_count > 0: watched to completion at least once — strong "
        "positive signal. view_count == 0 with last_viewed_at null on "
        "a released, downloaded item: in the library but never opened "
        "— negative signal (especially if the item has been available "
        "a while). view_count == 0 with last_viewed_at set: started "
        "but didn't finish — mild negative, the user dropped out. "
        "view_count is null: not in the library / not yet scanned — "
        "no signal either way. "
        "Defaults to the candidate's item_type; pass item_type='any' "
        "to span both."
    ),
    input_schema=CHECK_ADDED_HISTORY_SCHEMA,
    execute=_t_check_added_history,
)
