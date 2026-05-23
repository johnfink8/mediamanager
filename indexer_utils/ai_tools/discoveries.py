"""Subagent-backed discovery tools: window reports and per-title buzz lookup.

Unlike the other tools in this package, these make a nested LLM call rather
than reading project state. Each delegates to an inner Agent equipped with
the hosted ``WebSearchTool`` and returns the prose dossier directly to the
recommendation agent. No JSON post-processing — the consumer is another LLM
that reads prose fluently, so imposing a schema would just add latency, cost,
and parse-failure modes without helping the reader.
"""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Literal, Optional
from zoneinfo import ZoneInfo

from agents import Agent, RunConfig, RunContextWrapper, Runner
from agents.models.openai_provider import OpenAIProvider
from agents.tool import WebSearchTool
from decouple import config
from openai import AsyncOpenAI

from ..redis_client import get_redis_client, redis_get_json, redis_set_json
from .base import ToolContext
from .safe_tool import safe_tool
from .shared import enforce_result_budget

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"

logger = logging.getLogger(__name__)

# Box Office Mojo's weekend chart is stable enough that the cheapest
# mini tier handles it. Bump to "gpt-5.1" if research quality slips.
MODEL = "gpt-5.4-mini"

REPORT_CHAR_CAP = 12000

# Cache the dossier in Redis so repeated calls within a window don't pay the
# LLM + web_search cost. Bump the version suffix on prompt/schema changes.
CACHE_TTL_SECONDS = 6 * 60 * 60
CACHE_KEY_VERSION = "v1"

# US release day rolls over latest on the West Coast — Pacific keeps the
# cache key and the queried windows stable for a UTC host during late-Sunday-US
# hours, which would otherwise tip into Monday.
_TODAY_TZ = ZoneInfo("America/Los_Angeles")

# Loaded at import time, matching ``ai_recs.RECOMMENDATION_PROMPTS``.
_MOVIES_SYSTEM_PROMPT = (_PROMPTS_DIR / "search_recent_releases.md").read_text()
_TV_SYSTEM_PROMPT = (_PROMPTS_DIR / "search_recent_tv.md").read_text()
_BUZZ_SYSTEM_PROMPT = (_PROMPTS_DIR / "search_title_buzz.md").read_text()

_MOVIES_AGENT = Agent(
    name="search_recent_releases",
    model=MODEL,
    instructions=_MOVIES_SYSTEM_PROMPT,
    tools=[WebSearchTool()],
)
_TV_AGENT = Agent(
    name="search_recent_tv",
    model=MODEL,
    instructions=_TV_SYSTEM_PROMPT,
    tools=[WebSearchTool()],
)
_BUZZ_AGENT = Agent(
    name="search_title_buzz",
    model=MODEL,
    instructions=_BUZZ_SYSTEM_PROMPT,
    tools=[WebSearchTool()],
)


def _build_movies_prompt(
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


def _build_tv_prompt(
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
            f"Build a US TV-release dossier for the window "
            f"{window_start.isoformat()} through {window_end.isoformat()}."
        ),
        "",
        "Cover:",
        f"- The most recent Nielsen weekly streaming top 10 (top {top_n} "
        "rows; combine originals and acquired). For each row include "
        "rank, title, network/streamer, and viewership figure if "
        "published.",
        "- Series with new premieres in the window — full-season debuts "
        "or new-season debuts — with title, premiere date, "
        "network/streamer, and whether returning or brand-new.",
        "- Series wrapping with finales in the window; finales are "
        "often buzz peaks worth flagging.",
        "- A handful of notable limited-series or specialty premieres.",
        "- A 1-2 sentence summary of the headline release.",
        "",
        "Attach a quantitative signal to every title. For titles already "
        "on the Nielsen chart, the minutes-viewed figure already counts — "
        "don't double up. For all other titles, surface one of: IMDb "
        "rating + vote count (imdb.com/title/...), Rotten Tomatoes "
        "Tomatometer % + critic count and audience score "
        "(rottentomatoes.com/tv/...), or Metacritic Metascore + review "
        "count (metacritic.com/tv/...). Actively search for ratings on "
        "established shows that have aired ≥1 season — they almost "
        "always have IMDb scores. Reserve 'no rating found' for "
        "genuinely unrated titles (brand-new premieres yet to air, "
        "obscure regional series).",
        "",
        "Method: pull Nielsen's most recent weekly streaming top 10 "
        "from Variety or THR coverage, premiere/finale calendars from "
        f"Wikipedia's 'List of American television programs of {today.year}' "
        "or trade week-ahead recaps, IMDb Most Popular TV for "
        "supplementary buzz signal, and per-title ratings from IMDb, "
        "Rotten Tomatoes, or Metacritic.",
    ]
    if focus:
        parts.extend(["", f"Bias selections toward: {focus}."])
    return "\n".join(parts)


def _build_buzz_prompt(
    *,
    today: date,
    title: str,
    year: Optional[int],
    item_type: str,
) -> str:
    type_word = {"mv": "movie", "tv": "TV series"}.get(item_type, "title")
    year_part = f" ({year})" if year else ""
    return "\n".join(
        [
            f"Today is {today.isoformat()}.",
            (
                f"Look up reception and taste-adjacency for the "
                f"{type_word}: {title}{year_part}. The downstream agent is "
                f"matching this to a specific user's library, not asking "
                f"whether it's a good film — surface the signals that "
                f"question needs."
            ),
            "",
            "Cover:",
            "- Quantitative scores from primary sources where you can "
            "confirm them: Rotten Tomatoes (Tomatometer % + critic "
            "count + audience % + the Tomatometer consensus blurb), "
            "Metacritic (Metascore + review count + user score), "
            "IMDb (rating + vote count). Mark whichever you can't "
            "confirm 'no rating found' — never guess.",
            "- A 1-2 sentence critic consensus that captures what "
            "reviewers are actually saying, not just the number.",
            "- A 1-2 sentence audience reception read, especially "
            "where it diverges from critics.",
            "- Online buzz beyond aggregators: Reddit thread sentiment "
            "(r/movies, r/television, dedicated subreddits), "
            "Letterboxd (movies), or social/trade chatter. Capture "
            "the dominant takes — what people love, what they "
            "complain about. Quote phrases verbatim where it sharpens "
            "the read.",
            "- 3-6 taste-adjacent titles: works this is commonly "
            "compared to, recommended alongside, or cited next to. "
            "Pull from Rotten Tomatoes 'If you like…', Letterboxd "
            "'similar films', IMDb 'More like this', and recurring "
            "comparisons in reviews / Reddit / social chatter "
            "('X meets Y', 'for fans of Z'). Briefly note WHY each "
            "comparison is drawn — genre, tone, director, premise, "
            "shared cast.",
            "",
            "Method: hit Rotten Tomatoes, Metacritic, and IMDb pages "
            "for the title directly; sample relevant Reddit threads "
            "and Letterboxd reviews; cross-check with the Wikipedia "
            "reception section and one or two tier-1 trade reviews "
            "(NYT, Variety, THR, The Guardian) where they exist.",
        ]
    )


def _cache_key(
    *,
    prefix: str,
    today: date,
    weeks_back: int,
    weeks_forward: int,
    top_n: int,
    focus: Optional[str],
) -> str:
    return (
        f"mediamanager:{prefix}:{CACHE_KEY_VERSION}:"
        f"{today.isoformat()}:{weeks_back}:{weeks_forward}:"
        f"{top_n}:{focus or ''}"
    )


def _buzz_cache_key(
    *,
    today: date,
    title: str,
    year: Optional[int],
    item_type: str,
) -> str:
    return (
        f"mediamanager:search_title_buzz:{CACHE_KEY_VERSION}:"
        f"{today.isoformat()}:{item_type}:{title.strip().lower()}:"
        f"{year or ''}"
    )


async def _fetch_dossier(
    *,
    agent: Agent[Any],
    cache_key: str,
    user_prompt: str,
    today: date,
    log_tag: str,
) -> Dict[str, Any]:
    redis = get_redis_client()
    cached = redis_get_json(redis, cache_key)
    if isinstance(cached, dict) and "report" in cached:
        logger.info("%s cache hit key=%s", log_tag, cache_key)
        return cached

    # Per-call client so the httpx transport is bound to this event loop
    # and closed before the task exits — see indexer_utils/ai_tools/agent.py
    # for the same pattern in the parent loop.
    openai_client = AsyncOpenAI(api_key=config("OPENAI_API_KEY"))
    provider = OpenAIProvider(openai_client=openai_client)
    run_config = RunConfig(tracing_disabled=True, model_provider=provider)
    try:
        try:
            # max_turns is generous — the inner agent may run several
            # web_search rounds before producing the dossier. Two is enough in
            # practice but we give it four so a slow research path doesn't
            # tripwire.
            result = await Runner.run(
                agent,
                user_prompt,
                max_turns=4,
                run_config=run_config,
            )
        finally:
            # OpenAIProvider.aclose intentionally leaves the AsyncOpenAI
            # client open (in case it's shared), so we close it ourselves.
            await provider.aclose()
            await openai_client.close()
    except Exception as exc:
        logger.exception("%s subagent failed", log_tag)
        return {"error": f"{exc.__class__.__name__}: {exc}"}

    dossier = str(result.final_output or "").strip()
    if not dossier:
        return {"error": "subagent returned empty dossier"}

    payload = {"as_of": today.isoformat(), "report": dossier[:REPORT_CHAR_CAP]}
    redis_set_json(redis, cache_key, payload, CACHE_TTL_SECONDS)
    return payload


@safe_tool
async def search_recent_releases(
    wrapper: RunContextWrapper[ToolContext],
    weeks_back: int = 2,
    weeks_forward: int = 2,
    top_n: int = 20,
    focus: Optional[str] = None,
) -> Dict[str, Any]:
    """Check whether the candidate is a current theatrical release worth a closer look.

    Most catalog candidates are obscure and won't appear here at all; finding
    the candidate's title in the report is itself a strong positive signal —
    scan the weekend box-office chart, upcoming releases, and notable limited
    releases for it before anything else. Returns {as_of, report} where
    report is a prose dossier on US theatrical releases for a window around
    today (Box Office Mojo + Wikipedia, via a web-research subagent).

    Use when the candidate is a recent or soon-to-arrive theatrical film —
    not for catalog/streaming items. Window defaults to ±2 weeks; raise
    weeks_back / weeks_forward for a wider net. One call per session; the
    report is expensive (LLM + web fetches).

    Args:
        weeks_back: How many weeks before today to include (0-8).
        weeks_forward: How many weeks after today to include (0-8).
        top_n: Cap on the weekend chart size (1-30).
        focus: Optional free-text bias for the selection (e.g. 'horror',
            'awards contenders').
    """
    ctx = wrapper.context
    weeks_back = max(0, min(8, int(weeks_back or 2)))
    weeks_forward = max(0, min(8, int(weeks_forward or 2)))
    top_n = max(1, min(30, int(top_n or 20)))
    focus_clean = (focus or "").strip() or None

    today = datetime.now(_TODAY_TZ).date()
    user_prompt = _build_movies_prompt(
        today=today,
        weeks_back=weeks_back,
        weeks_forward=weeks_forward,
        top_n=top_n,
        focus=focus_clean,
    )
    cache_key = _cache_key(
        prefix="search_recent_releases",
        today=today,
        weeks_back=weeks_back,
        weeks_forward=weeks_forward,
        top_n=top_n,
        focus=focus_clean,
    )
    payload = await _fetch_dossier(
        agent=_MOVIES_AGENT,
        cache_key=cache_key,
        user_prompt=user_prompt,
        today=today,
        log_tag="search_recent_releases",
    )
    return enforce_result_budget(
        payload, "search_recent_releases", ctx.candidate.get("uid")
    )


@safe_tool
async def search_recent_tv(
    wrapper: RunContextWrapper[ToolContext],
    weeks_back: int = 2,
    weeks_forward: int = 2,
    top_n: int = 20,
    focus: Optional[str] = None,
) -> Dict[str, Any]:
    """Check whether the candidate is a current TV release worth a closer look.

    Most catalog candidates are obscure and won't appear here at all; finding
    the candidate's title in the report is itself a strong positive signal —
    scan the Nielsen streaming top 10, premiere calendar, and notable limited
    premieres for it before anything else. Returns {as_of, report} where
    report is a prose dossier on US TV releases and streaming buzz for a
    window around today (Nielsen via Variety/THR + Wikipedia, via a
    web-research subagent).

    Use when the candidate is a recent or soon-to-arrive TV series — not for
    movie items. Window defaults to ±2 weeks; raise weeks_back /
    weeks_forward for a wider net. One call per session; the report is
    expensive (LLM + web fetches).

    Args:
        weeks_back: How many weeks before today to include (0-8).
        weeks_forward: How many weeks after today to include (0-8).
        top_n: Cap on the streaming chart size (1-30).
        focus: Optional free-text bias for the selection.
    """
    ctx = wrapper.context
    weeks_back = max(0, min(8, int(weeks_back or 2)))
    weeks_forward = max(0, min(8, int(weeks_forward or 2)))
    top_n = max(1, min(30, int(top_n or 20)))
    focus_clean = (focus or "").strip() or None

    today = datetime.now(_TODAY_TZ).date()
    user_prompt = _build_tv_prompt(
        today=today,
        weeks_back=weeks_back,
        weeks_forward=weeks_forward,
        top_n=top_n,
        focus=focus_clean,
    )
    cache_key = _cache_key(
        prefix="search_recent_tv",
        today=today,
        weeks_back=weeks_back,
        weeks_forward=weeks_forward,
        top_n=top_n,
        focus=focus_clean,
    )
    payload = await _fetch_dossier(
        agent=_TV_AGENT,
        cache_key=cache_key,
        user_prompt=user_prompt,
        today=today,
        log_tag="search_recent_tv",
    )
    return enforce_result_budget(payload, "search_recent_tv", ctx.candidate.get("uid"))


@safe_tool
async def search_title_buzz(
    wrapper: RunContextWrapper[ToolContext],
    title: str,
    year: Optional[int] = None,
    item_type: Optional[Literal["mv", "tv"]] = None,
) -> Dict[str, Any]:
    """Deep-dive on reception and taste-adjacency for a specific title.

    Returns {as_of, report} where report is a prose dossier with Rotten
    Tomatoes / Metacritic / IMDb scores, critic and audience consensus, online
    chatter (Reddit, Letterboxd, trade reviews), AND 3–6 taste-adjacent titles
    the work is commonly compared to or recommended alongside — with the
    reason for each comparison so you can match against the user's library.

    Use when you want more than "is it on a current chart" — e.g. you need a
    reception read, a sense of where it lands with audiences vs critics, or
    (most useful here) a list of titles to cross-reference against what the
    user has already added. One call per candidate is plenty; the report is
    expensive (LLM + web fetches). Pass the year when known to disambiguate
    remakes and franchise entries.

    Args:
        title: Exact title to look up.
        year: Release year for disambiguation; strongly preferred.
        item_type: 'mv' for movie or 'tv' for series. Defaults to the
            recommendation agent's current item type.
    """
    ctx = wrapper.context
    title = (title or "").strip()
    if not title:
        return {"error": "title is required"}

    try:
        year_int: Optional[int] = int(year) if year is not None else None
    except (TypeError, ValueError):
        year_int = None

    resolved_type = item_type if item_type in ("mv", "tv") else ctx.item_type

    today = datetime.now(_TODAY_TZ).date()
    user_prompt = _build_buzz_prompt(
        today=today, title=title, year=year_int, item_type=resolved_type
    )
    cache_key = _buzz_cache_key(
        today=today, title=title, year=year_int, item_type=resolved_type
    )
    payload = await _fetch_dossier(
        agent=_BUZZ_AGENT,
        cache_key=cache_key,
        user_prompt=user_prompt,
        today=today,
        log_tag="search_title_buzz",
    )
    return enforce_result_budget(payload, "search_title_buzz", ctx.candidate.get("uid"))
