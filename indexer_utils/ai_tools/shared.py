"""Cross-cutting helpers used by the agent tool modules.

Three concerns live here:

1. Defensive clipping / result-budget enforcement so a single bloated DB row
   can't blow the model's context.
2. Decision summarization (``decision``, ``summarize_item``) — a single
   shape every search tool returns so the agent learns one vocabulary.
3. Shared row-filter logic (``row_passes_filters``) used by the three search
   tools, including the per-rating-source filters (imdb / tmdb / trakt / RT /
   metacritic). See ``~/.claude/projects/.../notes/rating-classifier-design.md``
   for the design discussion behind the per-source split.
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


# Per-rating-source fields actually populated in attrs.
#
# Coverage is item-type-asymmetric:
#   • MV: imdbuser/tmdbuser/traktuser/RT/Metacritic populated (12-34%);
#     ``rating_value`` is 0% (Radarr doesn't return ratings).
#   • TV: ``rating_value`` is the only populated field (34%, from Sonarr);
#     all other sources are 0%.
# Exposing both shapes lets the model pick whichever source has data.
#
# RT / Metacritic are critic aggregates so they have no votes companion (the
# corresponding ``*_votes`` field is always 0 across the corpus).
#
# TODO (see ~/.claude/projects/.../notes/rating-classifier-design.md): the
# right longer-term treatment is a calibrated classifier that synthesizes all
# sources including their absence flags. This per-source raw exposure is the
# interim step that also doubles as the feature surface for that classifier.
#
# (label, attrs_key_rating, attrs_key_votes). ``label`` is used both as the
# filter prefix (e.g. ``imdb_min``) and the output-key prefix in
# ``summarize_item`` (``imdb_rating``, ``imdb_votes``).
_RATING_SOURCES = [
    ("imdb", "imdbuser_value", "imdbuser_votes"),
    ("tmdb", "tmdbuser_value", "tmdbuser_votes"),
    ("trakt", "traktuser_value", "traktuser_votes"),
]
# Critic aggregates: 0-100 scale, no companion vote count.
_CRITIC_SOURCES = [
    ("rt", "rottenTomatoesuser_value"),
    ("metacritic", "metacriticuser_value"),
]


def _numeric(value: Any) -> Optional[float]:
    """Coerce a possibly-stringified number to float, else None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except (ValueError, AttributeError):
            return None
    return None


def summarize_item(item: IgnoreItem) -> Dict[str, Any]:
    attrs = item.attributes or {}
    summary: Dict[str, Any] = {
        "uid": item.uid,
        "title": item.title,
        "decision": decision(item),
        "genres": attrs_get_genres(attrs),
        "year": attrs.get("year"),
    }
    # Generic indexer-aggregate rating — populated for TV (Sonarr), 0% on MV.
    rating = _numeric(attrs.get("rating_value"))
    if rating is not None:
        summary["rating"] = rating
        votes = _numeric(attrs.get("rating_votes"))
        if votes is not None:
            summary["rating_votes"] = int(votes)
    # Per-source user ratings: only include sources that are actually
    # populated so the model doesn't burn tokens on null fields.
    for label, value_key, votes_key in _RATING_SOURCES:
        rating = _numeric(attrs.get(value_key))
        if rating is None:
            continue
        summary[f"{label}_rating"] = rating
        votes = _numeric(attrs.get(votes_key))
        if votes is not None:
            summary[f"{label}_votes"] = int(votes)
    # Critic-aggregate sources (0-100 scale, no companion votes field).
    for label, value_key in _CRITIC_SOURCES:
        score = _numeric(attrs.get(value_key))
        if score is not None:
            summary[label] = int(score)

    for key in ("network", "studio", "director", "runtime"):
        if attrs.get(key):
            summary[key] = attrs[key]
    lang = attrs.get("originalLanguage")
    if lang:
        summary["language"] = lang
    return summary


def row_passes_filters(item: IgnoreItem, filters: Dict[str, Any]) -> bool:
    """Return True if the row passes every specified filter. Missing data
    on the row counts as a fail for any filter that names that field —
    otherwise a search-by-runtime would silently include items with no
    runtime, which is misleading.

    Rating filters are per-source (``imdb_min``, ``imdb_votes_min``,
    ``tmdb_min``, ``tmdb_votes_min``, ``trakt_min``, ``trakt_votes_min``,
    ``rt_min``, ``metacritic_min``). Each checks the corresponding attrs
    field; the model picks the source it cares about for the query.
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

    runtime = _numeric(attrs.get("runtime"))
    if filters.get("runtime_min") is not None:
        if runtime is None or runtime < filters["runtime_min"]:
            return False
    if filters.get("runtime_max") is not None:
        if runtime is None or runtime > filters["runtime_max"]:
            return False

    # Generic indexer-aggregate rating (TV-side).
    if filters.get("rating_min") is not None:
        rating = _numeric(attrs.get("rating_value"))
        if rating is None or rating < filters["rating_min"]:
            return False
    if filters.get("rating_votes_min") is not None:
        votes = _numeric(attrs.get("rating_votes"))
        if votes is None or votes < filters["rating_votes_min"]:
            return False
    # Per-source rating + votes thresholds. Missing data on the row → fail
    # (consistent with the runtime / year behaviour).
    for label, value_key, votes_key in _RATING_SOURCES:
        if filters.get(f"{label}_min") is not None:
            rating = _numeric(attrs.get(value_key))
            if rating is None or rating < filters[f"{label}_min"]:
                return False
        if filters.get(f"{label}_votes_min") is not None:
            votes = _numeric(attrs.get(votes_key))
            if votes is None or votes < filters[f"{label}_votes_min"]:
                return False
    for label, value_key in _CRITIC_SOURCES:
        if filters.get(f"{label}_min") is not None:
            score = _numeric(attrs.get(value_key))
            if score is None or score < filters[f"{label}_min"]:
                return False

    year = _numeric(attrs.get("year"))
    if filters.get("year_min") is not None:
        if year is None or year < filters["year_min"]:
            return False
    if filters.get("year_max") is not None:
        if year is None or year > filters["year_max"]:
            return False

    return True


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
