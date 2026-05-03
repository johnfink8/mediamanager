"""Cross-cutting helpers used by the agent tool modules.

Three concerns live here:

1. Defensive clipping / result-budget enforcement so a single bloated DB row
   can't blow the model's context.
2. Decision summarization (``decision``, ``summarize_item``) — a single
   shape every search tool returns so the agent learns one vocabulary.
3. Shared filter primitives (``SHARED_FILTER_PROPS``, ``row_passes_filters``,
   ``extract_filters``) used by the three search tools.
"""

import json
import logging
from typing import Any, Dict, List, Optional

from ..models import IgnoreItem
from ..session import db_session

logger = logging.getLogger(__name__)


# Defensive caps. Every text field the model sees is clipped here so a single
# bloated DB row (an old verbose synopsis, a runaway recommendation reason,
# a 4 KB Plex summary, etc.) can't push the conversation past the model's
# context limit.
SYNOPSIS_CLIP = 480
REASON_CLIP = 240
PLEX_SUMMARY_CLIP = 320
CAST_LIMIT = 10
TOOL_RESULT_BUDGET_BYTES = 24_000


def clip(text: Optional[str], n: int) -> Optional[str]:
    if text is None:
        return None
    s = str(text)
    if len(s) <= n:
        return s
    return s[:n].rstrip() + " …"


def clip_list_of_strings(value: Any, n: int) -> Any:
    if isinstance(value, list):
        return [str(v) for v in value[:n]]
    return value


def _json_size(value: Any) -> int:
    try:
        return len(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return 0


def enforce_result_budget(
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
        clipped["synopsis"] = clip(clipped["synopsis"], 200)
    for list_field in ("results", "recently_watched", "recent_recommendations"):
        seq = clipped.get(list_field)
        if not isinstance(seq, list):
            continue
        for entry in seq:
            if not isinstance(entry, dict):
                continue
            if isinstance(entry.get("synopsis"), str):
                entry["synopsis"] = clip(entry["synopsis"], 160)
            if isinstance(entry.get("reason"), str):
                entry["reason"] = clip(entry["reason"], 160)

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


def attrs_get_genres(attrs: Optional[Dict[str, Any]]) -> List[str]:
    if not attrs:
        return []
    raw = attrs.get("genres")
    if isinstance(raw, list):
        return [str(g) for g in raw]
    if isinstance(raw, str):
        return [raw]
    return []


def decision(item: IgnoreItem) -> str:
    """Single field summarizing the user's decision on an item.

    Replaces the (added, ignored) pair the model previously had to reason
    through. ``ignored=True`` does NOT mean rejected — added items also have
    ``ignored=True`` once they've been processed. The model was conflating
    these.
    """
    if item.added:
        return "added"
    if item.ignore:
        return "rejected"
    return "pending"


def empty_decision_counts() -> Dict[str, int]:
    return {"added": 0, "rejected": 0, "pending": 0}


def summarize_item(item: IgnoreItem) -> Dict[str, Any]:
    attrs = item.attributes or {}
    summary: Dict[str, Any] = {
        "uid": item.uid,
        "title": item.title,
        "decision": decision(item),
        "genres": attrs_get_genres(attrs),
        "year": attrs.get("year"),
        "rating_value": attrs.get("rating_value"),
        "rating_votes": attrs.get("rating_votes"),
    }
    for key in ("network", "studio", "director", "runtime"):
        if attrs.get(key):
            summary[key] = attrs[key]
    lang = attrs.get("originalLanguage")
    if lang:
        summary["language"] = lang
    return summary


# ---------------------------------------------------------------------------
# Shared optional filters across search_by_genre / search_by_network /
# search_similar_by_synopsis. Each tool exposes the same set so the agent
# learns one filter vocabulary; missing fields on a row never match a
# numeric filter (we skip rather than match-by-default).
# ---------------------------------------------------------------------------

SHARED_FILTER_PROPS: Dict[str, Any] = {
    "language": {
        "type": "string",
        "description": (
            "Original language. Case-insensitive substring (e.g. 'en', "
            "'english', 'ja')."
        ),
    },
    "director": {
        "type": "string",
        "description": (
            "Director name. Case-insensitive substring. Movies only — "
            "TV rows have no director field."
        ),
    },
    "runtime_min": {
        "type": "integer",
        "minimum": 0,
        "description": "Min runtime in minutes (movies: total; TV: per-episode).",
    },
    "runtime_max": {
        "type": "integer",
        "minimum": 0,
        "description": "Max runtime in minutes.",
    },
    "rating_min": {
        "type": "number",
        "minimum": 0,
        "maximum": 10,
        "description": "Minimum rating value (0-10 scale).",
    },
    "votes_min": {
        "type": "integer",
        "minimum": 0,
        "description": (
            "Minimum rating-vote count. Use to suppress shows with high "
            "ratings backed by very few votes."
        ),
    },
    "year_min": {"type": "integer", "description": "Earliest release year."},
    "year_max": {"type": "integer", "description": "Latest release year."},
}


def row_passes_filters(item: IgnoreItem, filters: Dict[str, Any]) -> bool:
    """Return True if the row passes every specified filter. Missing data
    on the row counts as a fail for any filter that names that field —
    otherwise a search-by-runtime would silently include items with no
    runtime, which is misleading.
    """
    if not filters:
        return True
    attrs = item.attributes or {}

    if filters.get("language"):
        needle = str(filters["language"]).strip().lower()
        haystack = str(attrs.get("originalLanguage") or "").lower()
        if needle and needle not in haystack:
            return False

    if filters.get("director"):
        needle = str(filters["director"]).strip().lower()
        haystack = str(attrs.get("director") or "").lower()
        if needle and needle not in haystack:
            return False

    runtime = attrs.get("runtime")
    if filters.get("runtime_min") is not None:
        if not isinstance(runtime, (int, float)) or runtime < filters["runtime_min"]:
            return False
    if filters.get("runtime_max") is not None:
        if not isinstance(runtime, (int, float)) or runtime > filters["runtime_max"]:
            return False

    rating = attrs.get("rating_value")
    if filters.get("rating_min") is not None:
        if not isinstance(rating, (int, float)) or rating < filters["rating_min"]:
            return False

    votes = attrs.get("rating_votes")
    if filters.get("votes_min") is not None:
        if not isinstance(votes, (int, float)) or votes < filters["votes_min"]:
            return False

    year = attrs.get("year")
    if filters.get("year_min") is not None:
        if not isinstance(year, (int, float)) or year < filters["year_min"]:
            return False
    if filters.get("year_max") is not None:
        if not isinstance(year, (int, float)) or year > filters["year_max"]:
            return False

    return True


def extract_filters(input_: Dict[str, Any]) -> Dict[str, Any]:
    """Pull just the shared-filter keys out of a tool's input dict."""
    return {k: input_[k] for k in SHARED_FILTER_PROPS if input_.get(k) is not None}


def query_db_items(item_type: str, uids: List[str]) -> Dict[str, IgnoreItem]:
    if not uids:
        return {}
    with db_session() as session:
        rows = (
            session.query(IgnoreItem)
            .filter(IgnoreItem.item_type == item_type, IgnoreItem.uid.in_(uids))
            .all()
        )
        return {row.uid: row for row in rows}
