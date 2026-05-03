"""Search tools: synopsis (Weaviate), genre (DB), network/studio (DB).

All three return the same ``{results, decision_counts}`` shape so the agent
sees one vocabulary regardless of where the data came from.
"""

from typing import Any, Dict, List

from ..models import IgnoreItem
from ..session import db_session
from ..weaviate_client import asearch_by_synopsis
from .base import Tool, ToolContext, ToolResult
from .shared import (
    SHARED_FILTER_PROPS,
    attrs_get_genres,
    decision,
    empty_decision_counts,
    enforce_result_budget,
    extract_filters,
    query_db_items,
    row_passes_filters,
    summarize_item,
)

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
        **SHARED_FILTER_PROPS,
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
    db_rows = query_db_items(ctx.item_type, uids)
    filters = extract_filters(input_)

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
            # No DB row → can't evaluate filters → omit. This avoids
            # silently returning unfilterable hits when the agent asked
            # for a constraint.
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
            decision = summary["decision"]
            counts[decision] += 1
            for k, v in summary.items():
                if k not in ("uid", "title"):
                    item[k] = v
        results.append(item)
    output = {"results": results, "decision_counts": counts}
    return ToolResult(
        output=enforce_result_budget(
            output, "search_similar_by_synopsis", candidate_uid
        )
    )


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
        **SHARED_FILTER_PROPS,
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

    filters = extract_filters(input_)
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
    output = {"results": matches, "decision_counts": counts}
    return ToolResult(
        output=enforce_result_budget(output, "search_by_genre", candidate_uid)
    )


SEARCH_NETWORK_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "network": {
            "type": "string",
            "description": (
                "Network or studio name. Examples: 'Apple TV+', 'HBO', "
                "'Netflix', 'A24'. Case-insensitive substring match against "
                "network (TV) or studio (movies)."
            ),
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
        **SHARED_FILTER_PROPS,
    },
    "required": ["network"],
}


async def _t_search_network(input_: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    name_raw = str(input_.get("network") or "").strip()
    if not name_raw:
        return ToolResult(output={"error": "network is required"})
    needle = name_raw.lower()
    added_only = bool(input_.get("added_only"))
    limit = int(input_.get("limit") or 15)
    limit = max(1, min(limit, 50))

    filters = extract_filters(input_)
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
    output = {"results": matches, "decision_counts": counts}
    return ToolResult(
        output=enforce_result_budget(output, "search_by_network", candidate_uid)
    )


SEARCH_SYNOPSIS_TOOL = Tool(
    name="search_similar_by_synopsis",
    description=(
        "Free-text semantic search over the user's catalog. Use this "
        "to find items that resemble a vibe, theme, or premise (not "
        "exact metadata matches). Each row has a `decision` field "
        "(added | rejected | pending) plus a `decision_counts` "
        "summary across the result set."
    ),
    input_schema=SEARCH_SYNOPSIS_SCHEMA,
    execute=_t_search_synopsis,
)

SEARCH_GENRE_TOOL = Tool(
    name="search_by_genre",
    description=(
        "Find items in the user's catalog that match one or more "
        "genres. Use to gauge whether the user's added items overlap "
        "with the candidate's genres."
    ),
    input_schema=SEARCH_GENRE_SCHEMA,
    execute=_t_search_genre,
)

SEARCH_NETWORK_TOOL = Tool(
    name="search_by_network",
    description=(
        "Find items in the user's catalog by network (TV) or studio "
        "(movies). Examples: 'Apple TV+', 'HBO', 'A24'. Use this when "
        "the candidate has a distinctive platform — platform is often "
        "a strong positive or negative taste signal. Combine with "
        "added_only=true to see whether the user has historically "
        "liked output from that platform."
    ),
    input_schema=SEARCH_NETWORK_SCHEMA,
    execute=_t_search_network,
)
