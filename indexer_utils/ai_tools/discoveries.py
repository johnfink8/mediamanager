"""Subagent-backed discovery tool: theatrical release window report.

Unlike the other tools in this package, this one makes a nested LLM call
rather than reading project state. We hand a small research task to
OpenAI's Responses API with the hosted ``web_search`` tool enabled, ask
it to consult Box Office Mojo and Wikipedia, and return the prose
dossier directly to the recommendation agent. No JSON post-processing —
the consumer is another LLM that reads prose fluently, so imposing a
schema would just add latency, cost, and parse-failure modes without
helping the reader.
"""

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

from decouple import config
from openai import AsyncOpenAI

from ..redis_client import get_redis_client, redis_get_json, redis_set_json
from .base import Tool, ToolContext, ToolResult
from .shared import enforce_result_budget

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

logger = logging.getLogger(__name__)

# Box Office Mojo's weekend chart is stable enough that the cheapest
# mini tier handles it. Bump to "gpt-5.1" (same price tier as 4o was)
# if research quality on tricky weeks slips.
MODEL = "gpt-5.4-mini"

# Outer cap on the prose report. The recommendation loop's per-tool
# budget is ~24KB; a typical dossier is well under 4KB, so this is just
# a safety net for runaway responses.
REPORT_CHAR_CAP = 12000

# Cache the dossier in Redis so repeated calls within a window don't
# pay the LLM + web_search cost. The cache key includes ``today`` so
# the day-rollover gives us free invalidation; the TTL just bounds
# Redis memory and protects against within-day staleness if Box Office
# Mojo updates mid-day. Bump the version suffix on prompt/schema
# changes to flush old entries.
CACHE_TTL_SECONDS = 6 * 60 * 60
CACHE_KEY_VERSION = "v1"

SEARCH_RECENT_RELEASES_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "weeks_back": {
            "type": "integer",
            "minimum": 0,
            "maximum": 8,
            "default": 2,
            "description": "How many weeks before today to include.",
        },
        "weeks_forward": {
            "type": "integer",
            "minimum": 0,
            "maximum": 8,
            "default": 2,
            "description": "How many weeks after today to include for upcoming releases.",
        },
        "top_n": {
            "type": "integer",
            "minimum": 1,
            "maximum": 30,
            "default": 20,
            "description": "Cap on the weekend chart size.",
        },
        "focus": {
            "type": "string",
            "maxLength": 120,
            "description": (
                "Optional free-text bias for the selection (e.g. 'horror', "
                "'family-friendly', 'awards contenders'). Empty string for none."
            ),
        },
    },
}

# Loaded at import time, matching ``ai_recs.RECOMMENDATION_PROMPTS``. We
# inline the read instead of reusing ``ai_recs.load_prompt`` because
# importing from ai_recs would cycle through ``ai_tools/__init__``.
_SYSTEM_PROMPT = (_PROMPTS_DIR / "search_recent_releases.md").read_text()


def _build_prompt(
    *,
    today: date,
    weeks_back: int,
    weeks_forward: int,
    top_n: int,
    focus: Optional[str],
) -> str:
    window_start = today - timedelta(weeks=weeks_back)
    window_end = today + timedelta(weeks=weeks_forward)
    parts = [
        f"Today is {today.isoformat()}.",
        (
            f"Build a US theatrical-release dossier for the window "
            f"{window_start.isoformat()} through {window_end.isoformat()}."
        ),
        "",
        "Cover:",
        f"- The most recent weekend US box-office chart (top {top_n} "
        "rows). For each row include rank, title, weekend gross in "
        "whole US dollars, weeks in release, and distributor.",
        "- Theatrical releases scheduled in the window with title, "
        "release date, distributor, and whether wide or limited.",
        "- A handful of notable limited / festival / specialty "
        "releases worth flagging.",
        "- A 1-2 sentence summary of the headline release.",
        "",
        "Method: pull the weekend chart from Box Office Mojo "
        "(/weekend/YYYYWnn/), the release calendar from Box Office "
        f"Mojo (/calendar/YYYY-MM-DD/) for the window, and Wikipedia's "
        f"'List of American films of {today.year}' for cross-reference.",
    ]
    if focus:
        parts.extend(["", f"Bias selections toward: {focus}."])
    return "\n".join(parts)


def _cache_key(
    *,
    today: date,
    weeks_back: int,
    weeks_forward: int,
    top_n: int,
    focus: Optional[str],
) -> str:
    return (
        f"mediamanager:search_recent_releases:{CACHE_KEY_VERSION}:"
        f"{today.isoformat()}:{weeks_back}:{weeks_forward}:"
        f"{top_n}:{focus or ''}"
    )


async def _run_subagent(
    *,
    today: date,
    weeks_back: int,
    weeks_forward: int,
    top_n: int,
    focus: Optional[str],
) -> Dict[str, Any]:
    redis = get_redis_client()
    key = _cache_key(
        today=today,
        weeks_back=weeks_back,
        weeks_forward=weeks_forward,
        top_n=top_n,
        focus=focus,
    )
    cached = redis_get_json(redis, key)
    if isinstance(cached, dict) and "report" in cached:
        logger.info("search_recent_releases cache hit key=%s", key)
        return cached

    # Build a fresh AsyncOpenAI client per call. ai_recs.make_async_openai_client
    # has the same body, but importing from ai_recs would cycle through
    # ai_tools' own __init__. The httpx transport in AsyncOpenAI is bound to
    # the current event loop, so caching across calls is unsafe anyway.
    try:
        client = AsyncOpenAI(api_key=config("OPENAI_API_KEY"))
    except Exception as exc:
        logger.exception("failed to initialize subagent OpenAI client")
        return {"error": f"OpenAI client init failed: {exc}"}

    prompt = _build_prompt(
        today=today,
        weeks_back=weeks_back,
        weeks_forward=weeks_forward,
        top_n=top_n,
        focus=focus,
    )
    try:
        try:
            resp = await client.responses.create(
                model=MODEL,
                tools=[{"type": "web_search"}],
                instructions=_SYSTEM_PROMPT,
                input=prompt,
            )
        except Exception as exc:
            logger.exception("search_recent_releases subagent failed")
            return {"error": f"{exc.__class__.__name__}: {exc}"}
    finally:
        try:
            await client.close()
        except Exception:
            logger.exception("failed to close subagent AsyncOpenAI client")

    dossier = getattr(resp, "output_text", None) or ""
    if not dossier.strip():
        return {"error": "subagent returned empty dossier"}

    payload = {
        "as_of": today.isoformat(),
        "report": dossier[:REPORT_CHAR_CAP],
    }
    redis_set_json(redis, key, payload, CACHE_TTL_SECONDS)
    return payload


async def _t_search_recent_releases(
    input_: Dict[str, Any], ctx: ToolContext
) -> ToolResult:
    weeks_back = max(0, min(8, int(input_.get("weeks_back") or 2)))
    weeks_forward = max(0, min(8, int(input_.get("weeks_forward") or 2)))
    top_n = max(1, min(30, int(input_.get("top_n") or 20)))
    focus = (input_.get("focus") or "").strip() or None

    today = date.today()
    payload = await _run_subagent(
        today=today,
        weeks_back=weeks_back,
        weeks_forward=weeks_forward,
        top_n=top_n,
        focus=focus,
    )
    candidate_uid = ctx.candidate.get("uid") if ctx.candidate else None
    return ToolResult(
        output=enforce_result_budget(payload, "search_recent_releases", candidate_uid)
    )


SEARCH_RECENT_RELEASES_TOOL = Tool(
    name="search_recent_releases",
    description=(
        "Check whether the candidate is a current theatrical release "
        "worth a closer look. Most catalog candidates are obscure and "
        "won't appear here at all; finding the candidate's title in "
        "the report is itself a strong positive signal — scan the "
        "weekend box-office chart, upcoming releases, and notable "
        "limited releases for it before anything else. Returns "
        "{as_of, report} where report is a prose dossier on US "
        "theatrical releases for a window around today (Box Office "
        "Mojo + Wikipedia, via a web-research subagent). Use when the "
        "candidate is a recent or soon-to-arrive theatrical film — "
        "not for catalog/streaming items. Window defaults to ±2 "
        "weeks; raise weeks_back / weeks_forward for a wider net. "
        "One call per session; the report is expensive (LLM + web "
        "fetches)."
    ),
    input_schema=SEARCH_RECENT_RELEASES_SCHEMA,
    execute=_t_search_recent_releases,
)
