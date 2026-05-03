"""Subagent-backed discovery tools: window reports and per-title buzz lookup.

Unlike the other tools in this package, these make a nested LLM call
rather than reading project state. Each hands a small research task to
OpenAI's Responses API with the hosted ``web_search`` tool enabled, asks
it to consult domain-appropriate sources (Box Office Mojo + Wikipedia
for theatrical windows; Nielsen-via-Variety + Wikipedia for TV windows;
Rotten Tomatoes + Metacritic + IMDb + community discussion for the
per-title buzz lookup), and returns the prose dossier directly to the
recommendation agent. No JSON post-processing — the consumer is another
LLM that reads prose fluently, so imposing a schema would just add
latency, cost, and parse-failure modes without helping the reader.
"""

import logging
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, Optional
from zoneinfo import ZoneInfo

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
# Redis memory and protects against within-day source updates. Bump the
# version suffix on prompt/schema changes to flush old entries.
CACHE_TTL_SECONDS = 6 * 60 * 60
CACHE_KEY_VERSION = "v1"

# US release day rolls over latest on the West Coast — Pacific keeps
# the cache key and the queried windows stable for a UTC host during
# late-Sunday-US hours, which would otherwise tip into Monday.
_TODAY_TZ = ZoneInfo("America/Los_Angeles")


def _window_schema(top_n_description: str) -> Dict[str, Any]:
    return {
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
                "description": (
                    "How many weeks after today to include for upcoming releases."
                ),
            },
            "top_n": {
                "type": "integer",
                "minimum": 1,
                "maximum": 30,
                "default": 20,
                "description": top_n_description,
            },
            "focus": {
                "type": "string",
                "maxLength": 120,
                "description": (
                    "Optional free-text bias for the selection (e.g. 'horror', "
                    "'family-friendly', 'awards contenders'). "
                    "Empty string for none."
                ),
            },
        },
    }


SEARCH_RECENT_RELEASES_SCHEMA = _window_schema("Cap on the weekend chart size.")
SEARCH_RECENT_TV_SCHEMA = _window_schema("Cap on the streaming chart size.")

SEARCH_TITLE_BUZZ_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "Exact title to look up.",
        },
        "year": {
            "type": "integer",
            "minimum": 1900,
            "maximum": 2100,
            "description": (
                "Release year for disambiguation. Strongly preferred — "
                "remakes and franchise entries collide on title alone."
            ),
        },
        "item_type": {
            "type": "string",
            "enum": ["mv", "tv"],
            "description": (
                "Whether the title is a movie ('mv') or TV series ('tv'). "
                "Optional; defaults to the agent's current item type."
            ),
        },
    },
    "required": ["title"],
}

# Loaded at import time, matching ``ai_recs.RECOMMENDATION_PROMPTS``. We
# inline the read instead of reusing ``ai_recs.load_prompt`` because
# importing from ai_recs would cycle through ``ai_tools/__init__``.
_MOVIES_SYSTEM_PROMPT = (_PROMPTS_DIR / "search_recent_releases.md").read_text()
_TV_SYSTEM_PROMPT = (_PROMPTS_DIR / "search_recent_tv.md").read_text()
_BUZZ_SYSTEM_PROMPT = (_PROMPTS_DIR / "search_title_buzz.md").read_text()


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
                f"Look up review consensus, ratings, and online buzz "
                f"for the {type_word}: {title}{year_part}."
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
            "- 1-2 sentence overall verdict on whether a typical "
            "viewer in 2026 would find this worth their time.",
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
    cache_key: str,
    system_prompt: str,
    user_prompt: str,
    today: date,
    log_tag: str,
) -> Dict[str, Any]:
    redis = get_redis_client()
    cached = redis_get_json(redis, cache_key)
    if isinstance(cached, dict) and "report" in cached:
        logger.info("%s cache hit key=%s", log_tag, cache_key)
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

    try:
        try:
            resp = await client.responses.create(
                model=MODEL,
                tools=[{"type": "web_search"}],
                instructions=system_prompt,
                input=user_prompt,
            )
        except Exception as exc:
            logger.exception("%s subagent failed", log_tag)
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
    redis_set_json(redis, cache_key, payload, CACHE_TTL_SECONDS)
    return payload


PromptBuilder = Callable[..., str]


def _make_handler(
    *,
    name: str,
    system_prompt: str,
    prompt_builder: PromptBuilder,
) -> Callable[[Dict[str, Any], ToolContext], Awaitable[ToolResult]]:
    async def _handler(input_: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        weeks_back = max(0, min(8, int(input_.get("weeks_back") or 2)))
        weeks_forward = max(0, min(8, int(input_.get("weeks_forward") or 2)))
        top_n = max(1, min(30, int(input_.get("top_n") or 20)))
        focus = (input_.get("focus") or "").strip() or None

        today = datetime.now(_TODAY_TZ).date()
        user_prompt = prompt_builder(
            today=today,
            weeks_back=weeks_back,
            weeks_forward=weeks_forward,
            top_n=top_n,
            focus=focus,
        )
        cache_key = _cache_key(
            prefix=name,
            today=today,
            weeks_back=weeks_back,
            weeks_forward=weeks_forward,
            top_n=top_n,
            focus=focus,
        )
        payload = await _fetch_dossier(
            cache_key=cache_key,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            today=today,
            log_tag=name,
        )
        candidate_uid = ctx.candidate.get("uid") if ctx.candidate else None
        return ToolResult(output=enforce_result_budget(payload, name, candidate_uid))

    return _handler


_t_search_recent_releases = _make_handler(
    name="search_recent_releases",
    system_prompt=_MOVIES_SYSTEM_PROMPT,
    prompt_builder=_build_movies_prompt,
)

_t_search_recent_tv = _make_handler(
    name="search_recent_tv",
    system_prompt=_TV_SYSTEM_PROMPT,
    prompt_builder=_build_tv_prompt,
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
    applies_to=("mv",),
)


SEARCH_RECENT_TV_TOOL = Tool(
    name="search_recent_tv",
    description=(
        "Check whether the candidate is a current TV release worth a "
        "closer look. Most catalog candidates are obscure and won't "
        "appear here at all; finding the candidate's title in the "
        "report is itself a strong positive signal — scan the Nielsen "
        "streaming top 10, premiere calendar, and notable limited "
        "premieres for it before anything else. Returns {as_of, "
        "report} where report is a prose dossier on US TV releases "
        "and streaming buzz for a window around today (Nielsen via "
        "Variety/THR + Wikipedia, via a web-research subagent). Use "
        "when the candidate is a recent or soon-to-arrive TV series "
        "— not for movie items. Window defaults to ±2 weeks; raise "
        "weeks_back / weeks_forward for a wider net. One call per "
        "session; the report is expensive (LLM + web fetches)."
    ),
    input_schema=SEARCH_RECENT_TV_SCHEMA,
    execute=_t_search_recent_tv,
    applies_to=("tv",),
)


async def _t_search_title_buzz(input_: Dict[str, Any], ctx: ToolContext) -> ToolResult:
    title = (input_.get("title") or "").strip()
    if not title:
        return ToolResult(output={"error": "title is required"})

    year_raw = input_.get("year")
    try:
        year: Optional[int] = int(year_raw) if year_raw is not None else None
    except (TypeError, ValueError):
        year = None

    item_type = input_.get("item_type")
    if item_type not in ("mv", "tv"):
        item_type = ctx.item_type

    today = datetime.now(_TODAY_TZ).date()
    user_prompt = _build_buzz_prompt(
        today=today, title=title, year=year, item_type=item_type
    )
    cache_key = _buzz_cache_key(
        today=today, title=title, year=year, item_type=item_type
    )
    payload = await _fetch_dossier(
        cache_key=cache_key,
        system_prompt=_BUZZ_SYSTEM_PROMPT,
        user_prompt=user_prompt,
        today=today,
        log_tag="search_title_buzz",
    )
    candidate_uid = ctx.candidate.get("uid") if ctx.candidate else None
    return ToolResult(
        output=enforce_result_budget(payload, "search_title_buzz", candidate_uid)
    )


SEARCH_TITLE_BUZZ_TOOL = Tool(
    name="search_title_buzz",
    description=(
        "Deep-dive on review consensus and online buzz for a specific "
        "movie or TV title. Returns {as_of, report} where report is a "
        "prose dossier with Rotten Tomatoes / Metacritic / IMDb scores, "
        "the critic-consensus blurb, audience reception, and online "
        "chatter (Reddit, Letterboxd, trade reviews). Use when you "
        "need more than 'is it on a current chart' — e.g. the "
        "candidate looks promising and you want a reception read "
        "before recommending, or you're weighing how it lands with "
        "audiences vs critics, or comparing against a director's / "
        "showrunner's prior work. One call per candidate is plenty; "
        "the report is expensive (LLM + web fetches). Pass the year "
        "when known to disambiguate remakes and franchise entries."
    ),
    input_schema=SEARCH_TITLE_BUZZ_SCHEMA,
    execute=_t_search_title_buzz,
)
