"""Cast/director track-record subagent, at career scale.

``search_cast_history`` sends the candidate's top cast (billing order)
and director to a tool-bearing subagent. Each person's career sample — their
most prominent released works — is fetched from TMDB up front and embedded
in the payload, so the filmography never comes from the model's memory
(which invents credits). The subagent checks that sample against the
user's real Plex library with ``check_titles``; web research
(``brave_search`` / ``web_fetch``) is only the fallback for a person TMDB
can't resolve.
Matching is delegated to Plex's own search API (the same
``/hubs/search`` the Plex UI uses); the tool only verifies that a
returned result is the same title and year, so the subagent can never
invent possession. The dossier reports career-relative rates ("X of Y
works are in your library"), a per-person pattern label, and a net
pull-or-drag read.

The denominator is the person's actual output — never the user's catalog: the catalog indexes only a slice of a
career, so catalog-only rates undercount heavy followers, and a person
whose output was never indexed is invisible in it. The catalog
cross-reference is still computed and embedded as a seed (it is cheap and
useful), but it is explicitly not the denominator.

Watch history is deliberately out of scope here: having played a title
is not the same as having it, and rewatch counts would launder casual
viewing into "follows". Presence in the library is the only Plex signal
this subagent emits.
"""

import asyncio
import json
import logging
import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

from agents import Agent, RunConfig, RunContextWrapper, Runner
from agents.models.openai_provider import OpenAIProvider
from decouple import config
from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import bindparam, text

from ..plex_utils import hub_search
from ..redis_client import get_redis_client, redis_get_json, redis_set_json
from ..session import db_session
from ..tmdb import get_credit_person_ids, get_person_combined_credits, search_person_id
from .base import ToolContext
from .safe_tool import safe_tool
from .shared import strip_preamble
from .turn_budget import TurnBudget
from .webtools import brave_search, web_fetch

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
logger = logging.getLogger(__name__)

# Served locally; the gateway's only chat model (see OPENAI_BASE_URL).
MODEL = "qwen3.8"

# Loaded at import time, matching the discoveries subagents.
_SYSTEM_PROMPT = (_PROMPTS_DIR / "cast_history.md").read_text()

# The dossier goes stale when the user adds/deletes titles; 6h matches the
# discoveries and taste-signal era caches. Bump the version on prompt change.
CACHE_TTL_SECONDS = 6 * 60 * 60
CACHE_KEY_VERSION = "v7"

CAST_LIMIT = 10
TITLES_PER_PERSON_CAP = 15
PLEX_LOOKUP_CAP = 20
REPORT_CHAR_CAP = 4000
SUBAGENT_MAX_TURNS = 48
TITLES_PER_CHECK_CAP = 40

# The career sample handed to the subagent: each person's most prominent
# released works from TMDB. Prominence is TMDB vote count, but only over
# real roles — billed in the top 15 of a film, or a regular (3+ episodes)
# on a series; uncredited parts, "Self" appearances, and talk/news/reality
# shows never count.
FILMOGRAPHY_CAP = 25
MAX_BILLING_ORDER = 15
MIN_TV_EPISODES = 3
_NON_FICTION_GENRES = frozenset({10763, 10764, 10767})  # News, Reality, Talk
_SELF_ROLE = re.compile(r"\b(self|himself|herself|themselves)\b", re.IGNORECASE)

# A person's career spans features and series, and a work the user has can
# live in either library, so both hubs are consulted for every work.
PLEX_HUBS: tuple[str, ...] = ("movie", "show")

# For each person name: every same-type library title in which they appear in
# ``cast`` and/or as ``director``. The UNION dedupes a name present in both
# fields of one row; the EXISTS flags say which. Leave-one-out on uid so a
# previously added candidate doesn't "follow itself".
_CROSS_REF_SQL = """
SELECT lower(btrim(p.name)) AS person,
       i.uid,
       i.title,
       i.attributes->>'year' AS yr,
       i.attributes->>'tmdb_title' AS tmdb_title,
       i.added,
       EXISTS (SELECT 1
               FROM jsonb_array_elements_text(i.attributes->'cast') c
               WHERE lower(btrim(c)) = lower(btrim(p.name))) AS is_cast,
       lower(btrim(coalesce(i.attributes->>'director', '')))
           = lower(btrim(p.name)) AS is_dir
FROM indexer_utils_ignoreitem i
JOIN LATERAL (
    SELECT btrim(value) AS name
    FROM jsonb_array_elements_text(i.attributes->'cast')
    WHERE jsonb_typeof(i.attributes->'cast') = 'array'
    UNION
    SELECT btrim(i.attributes->>'director')
) p ON true
WHERE i.item_type = :it
  AND i.uid <> :uid
  AND btrim(p.name) <> ''
  AND lower(btrim(p.name)) IN :names
"""


class WorkQuery(BaseModel):
    """One career work to check: title plus optional year for disambiguation."""

    title: str
    year: Optional[int] = None


def _person_names(candidate: Dict[str, Any]) -> List[str]:
    """Top cast (billing order) + director, de-duped, first-seen casing kept."""
    out: List[str] = []
    seen = set()
    raw_cast = candidate.get("cast")
    if isinstance(raw_cast, list):
        for x in raw_cast[:CAST_LIMIT]:
            name = x.get("name") if isinstance(x, dict) else x
            name = str(name or "").strip()
            if name and name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
    director = candidate.get("director")
    directors = director if isinstance(director, list) else [director]
    for d in directors:
        name = str(d or "").strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


async def _cross_ref(
    item_type: str, names: List[str], exclude_uid: str
) -> Dict[str, Dict[str, Any]]:
    """Person (lowercased) → {titles, added} over the whole same-type library."""
    async with db_session() as session:
        rows = (
            await session.execute(
                text(_CROSS_REF_SQL).bindparams(bindparam("names", expanding=True)),
                {
                    "it": item_type,
                    "uid": exclude_uid,
                    "names": [n.lower() for n in names],
                },
            )
        ).all()
    out: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        try:
            year: int | None = int(r.yr) if r.yr is not None else None
        except (TypeError, ValueError):
            year = None
        role = "both" if (r.is_cast and r.is_dir) else ("dir" if r.is_dir else "cast")
        block = out.setdefault(r.person, {"titles": [], "added": 0})
        block["titles"].append(
            {
                "t": r.title,
                "y": year,
                "added": bool(r.added),
                "role": role,
                "tt": r.tmdb_title,
            }
        )
        if r.added:
            block["added"] += 1
    for block in out.values():
        block["titles"].sort(key=lambda t: t["y"] or 0, reverse=True)
    return out


def _filmography(
    credits: Dict[str, Any],
    *,
    acting: bool,
    directing: bool,
    today: date,
    exclude: Optional[tuple[str, int]] = None,
) -> Dict[str, Any]:
    """Notable released works from a TMDB ``combined_credits`` payload.

    Returns ``{"works": [{t, y, type}], "total": n}`` — up to
    ``FILMOGRAPHY_CAP`` works, newest first, and how many qualifying works
    TMDB lists in all. ``exclude`` is the candidate's own ``(media_type,
    tmdb_id)``: it is in every one of its people's credits, and the user
    doesn't have it yet, so it would count as a miss for each of them.
    """
    raw: List[Dict[str, Any]] = []
    if acting:
        for c in credits.get("cast") or []:
            character = str(c.get("character") or "")
            if "uncredited" in character.lower() or _SELF_ROLE.search(character):
                continue
            if c.get("media_type") == "movie":
                order = c.get("order")
                if order is not None and order >= MAX_BILLING_ORDER:
                    continue
            elif (c.get("episode_count") or 0) < MIN_TV_EPISODES:
                continue
            raw.append(c)
    if directing:
        raw.extend(c for c in credits.get("crew") or [] if c.get("job") == "Director")

    seen = set()
    works: List[Dict[str, Any]] = []
    for c in raw:
        media = c.get("media_type")
        key = (media, c.get("id"))
        released = str(c.get("release_date") or c.get("first_air_date") or "")
        if (
            media not in ("movie", "tv")
            or key == exclude
            or key in seen
            or _NON_FICTION_GENRES & set(c.get("genre_ids") or [])
            or not released[:4].isdigit()
            or released > today.isoformat()
        ):
            continue
        seen.add(key)
        works.append(
            {
                "t": c.get("title") or c.get("name"),
                "y": int(released[:4]),
                "type": media,
                "votes": c.get("vote_count") or 0,
            }
        )
    notable = sorted(works, key=lambda w: -int(w["votes"]))[:FILMOGRAPHY_CAP]
    notable.sort(key=lambda w: -int(w["y"]))
    return {
        "works": [{k: v for k, v in w.items() if k != "votes"} for w in notable],
        "total": len(works),
    }


async def _filmographies(
    item_type: str, candidate: Dict[str, Any], names: List[str]
) -> Dict[str, Optional[Dict[str, Any]]]:
    """Person (lowercased) → TMDB filmography, or None when unresolvable.

    People are resolved through the candidate's own TMDB credits when its
    ``tmdb_id`` is known, falling back to an exact-name person search. A
    failed lookup is None — no filmography, never an empty one.
    """
    ids: Dict[str, int] = {}
    tmdb_id = candidate.get("tmdb_id")
    if tmdb_id:
        try:
            ids = await asyncio.to_thread(
                get_credit_person_ids, item_type, int(tmdb_id)
            )
        except Exception:
            logger.exception("cast_history tmdb credits failed for %s", tmdb_id)

    director = candidate.get("director")
    directors = {
        str(d or "").strip().lower()
        for d in (director if isinstance(director, list) else [director])
    }
    in_cast = {
        str(x.get("name") if isinstance(x, dict) else x or "").strip().lower()
        for x in (candidate.get("cast") or [])[:CAST_LIMIT]
    }
    today = date.today()
    exclude = (
        ("movie" if item_type == "mv" else "tv", int(tmdb_id)) if tmdb_id else None
    )

    async def one(name: str) -> Optional[Dict[str, Any]]:
        key = name.lower()
        try:
            pid = ids.get(key) or await asyncio.to_thread(search_person_id, name)
            if pid is None:
                return None
            credits = await asyncio.to_thread(get_person_combined_credits, pid)
        except Exception:
            logger.exception("cast_history tmdb filmography failed for %s", name)
            return None
        return _filmography(
            credits,
            acting=key in in_cast,
            directing=key in directors,
            today=today,
            exclude=exclude,
        )

    results = await asyncio.gather(*(one(n) for n in names))
    return {n.lower(): r for n, r in zip(names, results)}


def _normalize_title(t: Any) -> str:
    """Lower-case, all non-alphanumerics stripped — for equality checks only."""
    return re.sub(r"[^a-z0-9]+", "", str(t or "").lower())


def _parse_year(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _entry_is_work(work: str, year: Optional[int], entry: Dict[str, Any]) -> bool:
    """Whether a hub-search result is the requested work.

    Plex's search already did the fuzzy part; this only guards against
    same-name collisions: same normalized title, and (when both are known)
    the same year ±1.
    """
    if _normalize_title(entry.get("title")) != _normalize_title(work):
        return False
    ey = _parse_year(entry.get("year"))
    if year is None or ey is None:
        return True
    return abs(ey - year) <= 1


def _plex_presence(work: str, year: Optional[int]) -> Dict[str, Any]:
    """Ask Plex's own search whether a work is in the user's library.

    Returns ``{"present": True, "plex_title", "plex_year"}`` when a
    hub-search result verifies as the same title (year ±1),
    ``{"present": False}`` when Plex answers and has no such item, or
    ``{"error": ...}`` when the lookup itself failed — that is no signal,
    not absence, and must never be read as a negative.
    """
    if not _normalize_title(work):
        return {"present": False}
    for hub in PLEX_HUBS:
        try:
            entries = hub_search(work, hub)
        except Exception as exc:
            return {"error": f"plex {hub} search failed: {exc.__class__.__name__}"}
        for entry in entries:
            if _entry_is_work(work, year, entry):
                return {
                    "present": True,
                    "plex_title": entry.get("title") or "",
                    "plex_year": _parse_year(entry.get("year")),
                }
    return {"present": False}


async def _plex_annotate(item_type: str, titles: List[Dict[str, Any]]) -> None:
    """Attach live Plex status to the most signal-bearing seed titles (in place).

    ``in_library`` when Plex's search reports the title (follow evidence
    even if the item never went through the curator); ``missing`` when the
    user added it but Plex has no such title (deleted — a strong negative).
    Never-added titles get no key: absence carries no signal. Plex is
    only tracked for movies upstream.

    Queries go through Plex's own search API and the result must verify
    as the same title (year ±1), so a raw release filename can never be
    mistaken for a match. Infrastructure failures leave the plex axis
    empty rather than fabricating a "missing".
    """
    if item_type != "mv" or not titles:
        return

    def _priority(t: Dict[str, Any]) -> tuple[int, int]:
        return (0 if t["added"] else 1, -(t["y"] or 0))

    picked = sorted(titles, key=_priority)[:PLEX_LOOKUP_CAP]

    async def _one(t: Dict[str, Any]) -> None:
        clean = t.get("tt") or ""
        if not clean:
            return
        res = await asyncio.to_thread(_plex_presence, clean, t["y"])
        if "error" in res:
            logger.warning(
                "cast_history plex lookup failed for %s: %s", clean, res["error"]
            )
            return
        if res["present"]:
            t["plex"] = "in_library"
        elif t["added"]:
            t["plex"] = "missing"

    await asyncio.gather(*(_one(t) for t in picked))


def _user_prompt(
    item_type: str,
    candidate: Dict[str, Any],
    blocks: Dict[str, Dict[str, Any]],
    filmographies: Dict[str, Optional[Dict[str, Any]]],
) -> str:
    """The model-facing payload: candidate + every person in the people-set.

    Each person carries the catalog seed — a slice of what the user has,
    with live Plex presence where known — which is a head start, never
    the denominator. People with no catalog rows get an empty seed; the
    subagent establishes their career over the web.
    """
    people: Dict[str, Any] = {}
    for name in _person_names(candidate):
        key = name.lower()
        block = blocks.get(key)
        if block is None:
            people[key] = {
                "name": name,
                "filmography": filmographies.get(key),
                "catalog": 0,
                "added": 0,
                "titles": [],
                "more": 0,
            }
            continue
        shown = []
        for t in block["titles"][:TITLES_PER_PERSON_CAP]:
            row = {
                "t": t["t"],
                "y": t["y"],
                "added": t["added"],
                "role": t["role"],
            }
            if t.get("tt"):
                row["tt"] = t["tt"]
            if "plex" in t:
                row["plex"] = t["plex"]
            shown.append(row)
        people[key] = {
            "name": name,
            "filmography": filmographies.get(key),
            "catalog": len(block["titles"]),
            "added": block["added"],
            "titles": shown,
            "more": len(block["titles"]) - len(shown),
        }
    payload = {
        "candidate": {
            "item_type": item_type,
            "uid": candidate.get("uid"),
            "title": candidate.get("title"),
            "year": candidate.get("year"),
        },
        "people": people,
        "seed_note": (
            "Each person's 'filmography' (from TMDB) is the career sample: "
            "check those works, exactly as given, with check_titles. The "
            "catalog fields ('titles', 'catalog', 'added', 'more') are the "
            "indexed-catalog seed only — context, not the sample."
        ),
    }
    return json.dumps(payload, default=str)


@safe_tool
async def check_titles(
    wrapper: RunContextWrapper[ToolContext],
    titles: List[WorkQuery],
) -> Dict[str, Any]:
    """Check whether the proposed works are in the user's Plex library.

    Matching is delegated to Plex's own search API and the result must
    verify as the same title (year ±1), so report the output as fact.
    Per work:
      - ``present``: true when the work is in the user's library (plus
        ``plex_title`` / ``plex_year`` as Plex spells them); false when
        Plex answers and the work is not there — absence is a finding,
        not a failure.
      - ``error``: the lookup itself failed; that work carries no signal,
        do not count it either way.

    Args:
        titles: Up to 40 works, each a {title, year} object.
    """
    ctx = wrapper.context

    async def one(work: WorkQuery) -> Dict[str, Any]:
        title = str(work.title or "").strip()
        res = await asyncio.to_thread(_plex_presence, title, work.year)
        out: Dict[str, Any] = {"title": title, "year": work.year}
        if "error" in res:
            out["error"] = res["error"]
        else:
            out["present"] = res["present"]
            if res["present"]:
                out["plex_title"] = res["plex_title"]
                out["plex_year"] = res["plex_year"]
        return out

    works = [w for w in titles[:TITLES_PER_CHECK_CAP] if str(w.title or "").strip()]
    results = await asyncio.gather(*(one(w) for w in works))
    return {"item_type": ctx.item_type, "results": results}


async def _web_search(
    wrapper: RunContextWrapper[ToolContext],
    query: str,
    count: int = 10,
    freshness: str = "",
) -> Dict[str, Any]:
    """Search the web — alias for brave_search.

    Some models insist on the name ``web_search`` regardless of the tool
    list; exposing both avoids a wasted turn on a "tool not found" error.
    """
    raw = getattr(brave_search, "__wrapped__")
    return cast(
        Dict[str, Any],
        await raw(wrapper, query=query, count=count, freshness=freshness),
    )


web_search = safe_tool(_web_search, name_override="web_search")


_CAST_AGENT = Agent(
    name="cast_history",
    model=MODEL,
    instructions=_SYSTEM_PROMPT,
    tools=[brave_search, web_search, web_fetch, check_titles],
)


async def _fetch_dossier(
    cache_key: str, user_prompt: str, ctx: ToolContext
) -> Dict[str, Any]:
    """Run the research subagent and cache its dossier under ``cache_key``.

    ``ctx`` is passed through so ``check_titles`` can reach the user's
    Plex via the tool context.
    """
    # Per-call client so the httpx transport is bound to this event loop
    # and closed before the task exits — see agent.py.
    openai_client = AsyncOpenAI(
        api_key=config("OPENAI_API_KEY"),
        base_url=config("OPENAI_BASE_URL", default=None),
    )
    provider = OpenAIProvider(openai_client=openai_client)
    run_config = RunConfig(tracing_disabled=True, model_provider=provider)
    try:
        try:
            result = await Runner.run(
                TurnBudget(SUBAGENT_MAX_TURNS).prepare(_CAST_AGENT, provider),
                user_prompt,
                context=ctx,
                max_turns=SUBAGENT_MAX_TURNS,
                run_config=run_config,
            )
        finally:
            await provider.aclose()
            await openai_client.close()
    except Exception as exc:
        logger.exception("cast_history subagent failed")
        return {"error": f"{exc.__class__.__name__}: {exc}"}

    dossier = strip_preamble(str(result.final_output or "").strip())
    if not dossier:
        return {
            "error": "subagent returned no dossier "
            f"(responses={len(result.raw_responses)}, "
            f"max_turns={SUBAGENT_MAX_TURNS})"
        }
    if len(dossier) > REPORT_CHAR_CAP:
        dossier = dossier[: REPORT_CHAR_CAP - 1] + "\u2026"

    payload = {"as_of": date.today().isoformat(), "report": dossier[:REPORT_CHAR_CAP]}
    redis_set_json(get_redis_client(), cache_key, payload, CACHE_TTL_SECONDS)
    return payload


@safe_tool
async def search_cast_history(
    wrapper: RunContextWrapper[ToolContext],
) -> Dict[str, Any]:
    """Cast/director track record of the candidate, at career scale.

    For each of the candidate's top cast (billing order) and director, the
    person's most prominent works (from TMDB) are checked against the
    user's Plex library by a subagent. Returns a prose dossier: per-person
    career-relative rates ("X of Y works are in your library"), a pattern
    label (followed / selective / avoided / no evidence), and a net
    pull-or-drag verdict.

    Career-relative rates supersede the taste_signal cast_xref counts:
    call this whenever cast or director is even a secondary lane — it is
    the difference between "4 of the 5 in our catalog" and "29 of 30 in
    their career".

    Returns:
        dict: {"report": str} on success; {"no_cast_data": true} when the
            candidate has no cast or director metadata; {"error": str} on
            subagent failure.
    """
    ctx = wrapper.context
    candidate = ctx.candidate
    names = _person_names(candidate)
    if not names:
        return {"no_cast_data": True}

    cache_key = (
        f"mediamanager:cast_history:{CACHE_KEY_VERSION}:{ctx.item_type}:"
        f"{candidate.get('uid')}"
    )
    cached = redis_get_json(get_redis_client(), cache_key)
    if isinstance(cached, dict) and "report" in cached:
        logger.info("cast_history cache hit key=%s", cache_key)
        return cached

    blocks, filmographies = await asyncio.gather(
        _cross_ref(ctx.item_type, names, str(candidate.get("uid") or "")),
        _filmographies(ctx.item_type, candidate, names),
    )
    await _plex_annotate(
        ctx.item_type, [t for b in blocks.values() for t in b["titles"]]
    )
    user_prompt = _user_prompt(ctx.item_type, candidate, blocks, filmographies)
    return await _fetch_dossier(cache_key, user_prompt, ctx)
