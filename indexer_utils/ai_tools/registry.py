"""Tool registry for the recommendation agent.

Five tools are exposed to the model:

- ``search_similar_by_synopsis`` — semantic free-text query over the user's
  catalog (Weaviate near_text). Source-agnostic from the model's POV.
- ``search_by_genre`` — DB filter over IgnoreItem attributes by genre, scoped
  to the candidate's item_type.
- ``get_item_details`` — IgnoreItem row + Plex view/rating fields for movies.
- ``get_user_history`` — recent Plex plays + recent recommendation feedback.
- ``submit_recommendation`` — terminal tool that ends the agent loop with the
  final verdict.

Tools are kept opaque to data source (the model doesn't know what's Plex vs
Weaviate vs Radarr) so we can expand the surface without retraining prompts.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from ..models import IgnoreItem, MovieRecommendationRecord
from ..plex_utils import aget_plex_details, aget_recently_played
from ..session import db_session
from ..weaviate_client import asearch_by_synopsis
from .base import TerminalToolResult, Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())


# Defensive caps. Every text field the model sees is clipped here so a single
# bloated DB row (an old verbose synopsis, a runaway recommendation reason,
# a 4 KB Plex summary, etc.) can't push the conversation past the model's
# context limit.
SYNOPSIS_CLIP = 480
REASON_CLIP = 240
PLEX_SUMMARY_CLIP = 320
CAST_LIMIT = 10
TOOL_RESULT_BUDGET_BYTES = 24_000


def _clip(text: Optional[str], n: int) -> Optional[str]:
    if text is None:
        return None
    s = str(text)
    if len(s) <= n:
        return s
    return s[:n].rstrip() + " …"


def _clip_list_of_strings(value: Any, n: int) -> Any:
    if isinstance(value, list):
        return [str(v) for v in value[:n]]
    return value


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return 0


def _enforce_result_budget(
    output: Any, tool_name: str, candidate_uid: Optional[str]
) -> Any:
    """If a tool's JSON output exceeds the budget, drop or further-clip the
    heaviest fields and add a ``_clipped`` marker so the model knows context
    was trimmed. Best-effort safety net for unexpectedly bloated DB rows.
    """
    size = _json_size(output)
    if size <= TOOL_RESULT_BUDGET_BYTES:
        return output

    if not isinstance(output, dict):
        return output

    original_size = size
    clipped: Dict[str, Any] = dict(output)

    # Pass 1: drop the heaviest known offenders.
    if "plex_extras" in clipped and isinstance(clipped["plex_extras"], dict):
        extras = dict(clipped["plex_extras"])
        extras.pop("summary", None)
        clipped["plex_extras"] = extras
    if isinstance(clipped.get("synopsis"), str):
        clipped["synopsis"] = _clip(clipped["synopsis"], 200)
    for list_field in ("results", "recently_watched", "recent_recommendations"):
        seq = clipped.get(list_field)
        if not isinstance(seq, list):
            continue
        for entry in seq:
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("synopsis"), str):
                entry["synopsis"] = _clip(entry["synopsis"], 160)
            if isinstance(entry.get("reason"), str):
                entry["reason"] = _clip(entry["reason"], 160)

    # Pass 2: if still over budget, halve the longest list field repeatedly.
    while _json_size(clipped) > TOOL_RESULT_BUDGET_BYTES:
        list_fields = [(k, v) for k, v in clipped.items() if isinstance(v, list) and v]
        if not list_fields:
            break
        longest_key, longest_seq = max(list_fields, key=lambda kv: len(kv[1]))
        new_len = max(1, len(longest_seq) // 2)
        clipped[longest_key] = longest_seq[:new_len]
        if new_len == 1 and _json_size(clipped) > TOOL_RESULT_BUDGET_BYTES:
            # Even one-entry lists are too big; bail out.
            break

    clipped["_clipped"] = True
    final_size = _json_size(clipped)
    logger.warning(
        "tool=%s candidate=%s result %d bytes exceeds %d budget; clipped to %d",
        tool_name,
        candidate_uid,
        original_size,
        TOOL_RESULT_BUDGET_BYTES,
        final_size,
    )
    return clipped


def _attrs_get_genres(attrs: Optional[Dict[str, Any]]) -> List[str]:
    if not attrs:
        return []
    raw = attrs.get("genres")
    if isinstance(raw, list):
        return [str(g) for g in raw]
    if isinstance(raw, str):
        return [raw]
    return []


def _summarize_item(item: IgnoreItem) -> Dict[str, Any]:
    attrs = item.attributes or {}
    return {
        "uid": item.uid,
        "title": item.title,
        "added": bool(item.added),
        "ignored": bool(item.ignore),
        "genres": _attrs_get_genres(attrs),
        "year": attrs.get("year"),
        "rating_value": attrs.get("rating_value"),
        "rating_votes": attrs.get("rating_votes"),
    }


def _query_db_items(item_type: str, uids: List[str]) -> Dict[str, IgnoreItem]:
    if not uids:
        return {}
    with db_session() as session:
        rows = (
            session.query(IgnoreItem)
            .filter(IgnoreItem.item_type == item_type, IgnoreItem.uid.in_(uids))
            .all()
        )
        return {row.uid: row for row in rows}


# ---------------------------------------------------------------------------
# search_similar_by_synopsis
# ---------------------------------------------------------------------------

SEARCH_SYNOPSIS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Free-text description of the kind of item you want to find "
                "neighbors for. Examples: 'gritty cold-war espionage thriller', "
                "'animated coming-of-age comedy with strong female lead'."
            ),
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 25,
            "description": "Max results. Default 10.",
        },
    },
    "required": ["query"],
}


async def _t_search_synopsis(input_: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    query = str(input_.get("query") or "").strip()
    if not query:
        return ToolResult(output={"error": "query is required"})
    limit = int(input_.get("limit") or 10)
    limit = max(1, min(limit, 25))

    raw = await asearch_by_synopsis(query, limit, ctx.item_type)
    uids = [r["uid"] for r in raw if r.get("uid")]
    db_rows = _query_db_items(ctx.item_type, uids)

    candidate_uid = ctx.candidate.get("uid")
    results: List[Dict[str, Any]] = []
    for hit in raw:
        uid = hit.get("uid")
        if not uid or uid == candidate_uid:
            continue
        row = db_rows.get(uid)
        item: Dict[str, Any] = {
            "uid": uid,
            "title": hit.get("title"),
            "distance": (
                round(hit["distance"], 4) if hit.get("distance") is not None else None
            ),
        }
        if row is not None:
            item.update(
                {
                    "added": bool(row.added),
                    "ignored": bool(row.ignore),
                    "genres": _attrs_get_genres(row.attributes),
                    "year": (row.attributes or {}).get("year"),
                }
            )
        results.append(item)
    output = {"results": results}
    return ToolResult(
        output=_enforce_result_budget(
            output, "search_similar_by_synopsis", candidate_uid
        )
    )


# ---------------------------------------------------------------------------
# search_by_genre
# ---------------------------------------------------------------------------

SEARCH_GENRE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "genres": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "description": "Genre names. Items matching ANY genre are returned.",
        },
        "added_only": {
            "type": "boolean",
            "description": "If true, restrict to items the user added. Default false.",
        },
        "limit": {
            "type": "integer",
            "minimum": 1,
            "maximum": 50,
            "description": "Max results. Default 15.",
        },
    },
    "required": ["genres"],
}


async def _t_search_genre(input_: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    genres_raw = input_.get("genres") or []
    if not isinstance(genres_raw, list) or not genres_raw:
        return ToolResult(output={"error": "genres must be a non-empty array"})
    wanted = {str(g).strip().lower() for g in genres_raw if str(g).strip()}
    if not wanted:
        return ToolResult(output={"error": "no valid genres provided"})
    added_only = bool(input_.get("added_only"))
    limit = int(input_.get("limit") or 15)
    limit = max(1, min(limit, 50))

    candidate_uid = ctx.candidate.get("uid")
    matches: List[Dict[str, Any]] = []
    with db_session() as session:
        q = session.query(IgnoreItem).filter(IgnoreItem.item_type == ctx.item_type)
        if added_only:
            q = q.filter(IgnoreItem.added.is_(True))
        for row in q.all():
            if row.uid == candidate_uid:
                continue
            row_genres = {g.lower() for g in _attrs_get_genres(row.attributes)}
            if not (row_genres & wanted):
                continue
            matches.append(_summarize_item(row))
            if len(matches) >= limit:
                break
    output = {"results": matches, "count": len(matches)}
    return ToolResult(
        output=_enforce_result_budget(output, "search_by_genre", candidate_uid)
    )


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
            "added": added_flag,
            "ignored": bool(row.ignore),
            "genres": _attrs_get_genres(attrs),
            "year": attrs.get("year"),
            "cast": _clip_list_of_strings(attrs.get("cast"), CAST_LIMIT),
            "network": attrs.get("network"),
            "language": attrs.get("originalLanguage"),
            "rating_value": attrs.get("rating_value"),
            "rating_votes": attrs.get("rating_votes"),
            "synopsis": _clip(ai.get("synopsis"), SYNOPSIS_CLIP),
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
            # Plex summary fields can be a few KB each. Clip aggressively
            # so a handful of get_item_details calls can't fill the context.
            if "summary" in extras:
                extras["summary"] = _clip(extras["summary"], PLEX_SUMMARY_CLIP)
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
        output=_enforce_result_budget(
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
                    "reason": _clip(r.recommended_reason, REASON_CLIP),
                }
            )
    except Exception:
        logger.exception("get_user_history: recommendation history fetch failed")

    output = {
        "recently_watched": plex_history,
        "recent_recommendations": rec_history,
    }
    return ToolResult(
        output=_enforce_result_budget(
            output, "get_user_history", ctx.candidate.get("uid")
        )
    )


# ---------------------------------------------------------------------------
# submit_recommendation (terminal)
# ---------------------------------------------------------------------------

SUBMIT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "recommend": {
            "type": "boolean",
            "description": "True if this candidate should be surfaced to the user.",
        },
        "score": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "Confidence/strength of fit. 0.0=poor, 1.0=ideal.",
        },
        "reason": {
            "type": "string",
            "maxLength": 240,
            "description": "Short justification — single strongest signal.",
        },
    },
    "required": ["recommend", "score", "reason"],
}


async def _t_submit(input_: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        rec = bool(input_.get("recommend"))
        score = float(input_.get("score") or 0.0)
        score = max(0.0, min(score, 1.0))
        reason = str(input_.get("reason") or "")[:240]
    except (TypeError, ValueError) as exc:
        return ToolResult(output={"error": f"invalid submission: {exc}"})
    return TerminalToolResult(
        output={"recommend": rec, "score": score, "reason": reason}
    )


def build_registry() -> Dict[str, Tool]:
    tools = [
        Tool(
            name="search_similar_by_synopsis",
            description=(
                "Free-text semantic search over the user's catalog. Use this to "
                "find items that resemble a vibe, theme, or premise (not exact "
                "metadata matches). Returns titles with their added/ignored "
                "status so you can read the user's taste signal."
            ),
            input_schema=SEARCH_SYNOPSIS_SCHEMA,
            execute=_t_search_synopsis,
        ),
        Tool(
            name="search_by_genre",
            description=(
                "Find items in the user's catalog that match one or more "
                "genres. Use to gauge whether the user's added items overlap "
                "with the candidate's genres."
            ),
            input_schema=SEARCH_GENRE_SCHEMA,
            execute=_t_search_genre,
        ),
        Tool(
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
        ),
        Tool(
            name="get_user_history",
            description=(
                "Recent user activity: items recently watched and prior "
                "recommendation feedback (LIKE / NOT_NOW / NEVER). Use to "
                "calibrate against current taste rather than stale catalog state."
            ),
            input_schema=GET_HISTORY_SCHEMA,
            execute=_t_get_history,
        ),
        Tool(
            name="submit_recommendation",
            description=(
                "Submit your final verdict. ALWAYS call this exactly once when "
                "you have enough context — this ends the session. Do not call "
                "any other tools after this one."
            ),
            input_schema=SUBMIT_SCHEMA,
            execute=_t_submit,
            is_terminal=True,
        ),
    ]
    return {t.name: t for t in tools}


REGISTRY: Dict[str, Tool] = build_registry()
