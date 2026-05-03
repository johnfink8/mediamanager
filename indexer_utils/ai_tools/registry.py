"""Tool registry for the recommendation agent.

Tools exposed to the model:

- ``search_similar_by_synopsis`` — semantic free-text query (Weaviate).
- ``search_by_genre`` — DB filter over IgnoreItem attributes by genre.
- ``search_by_network`` — DB filter by network (TV) or studio (movies).
- ``get_item_details`` — IgnoreItem row + Plex view/rating fields for movies.
- ``get_user_history`` — recent Plex plays + recent recommendation feedback.
- ``check_added_history`` — Radarr/Sonarr download state + Plex views for
  previously-added items, gated on release date.
- ``search_recent_releases`` — subagent-backed report on US theatrical
  releases in a window around today (Box Office Mojo + Wikipedia).
- ``submit_recommendation`` — terminal tool; ends the agent loop.

Implementations live in sibling modules: ``searches`` (the three search
tools), ``inspections`` (Plex/Radarr/Sonarr lookup tools), ``discoveries``
(outward-looking research subagents), and ``_shared`` (clipping, budget
enforcement, decision/filter primitives). This module just composes the
registry.
"""

from typing import Any, Dict

from .base import TerminalToolResult, Tool, ToolContext, ToolResult
from .discoveries import SEARCH_RECENT_RELEASES_TOOL
from .inspections import (
    CHECK_ADDED_HISTORY_TOOL,
    GET_DETAILS_TOOL,
    GET_HISTORY_TOOL,
)
from .searches import SEARCH_GENRE_TOOL, SEARCH_NETWORK_TOOL, SEARCH_SYNOPSIS_TOOL

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


SUBMIT_TOOL = Tool(
    name="submit_recommendation",
    description=(
        "Submit your final verdict. ALWAYS call this exactly once when "
        "you have enough context — this ends the session. Do not call "
        "any other tools after this one."
    ),
    input_schema=SUBMIT_SCHEMA,
    execute=_t_submit,
    is_terminal=True,
)


REGISTRY: Dict[str, Tool] = {
    t.name: t
    for t in (
        SEARCH_SYNOPSIS_TOOL,
        SEARCH_GENRE_TOOL,
        SEARCH_NETWORK_TOOL,
        GET_DETAILS_TOOL,
        GET_HISTORY_TOOL,
        CHECK_ADDED_HISTORY_TOOL,
        SEARCH_RECENT_RELEASES_TOOL,
        SUBMIT_TOOL,
    )
}
