"""Cast/director track-record subagent.

``search_cast_history`` cross-references the candidate's top cast (billing
order) and director against the user's whole same-type library — for each
person, every title they appear in (``cast`` and/or ``director``) with added
and plex status — and hands that table to an inner agent that returns a prose
dossier: per-person stats, a pattern label (followed across the board /
selective / actively avoided / no evidence), and a net pull-or-drag read.

Unlike the discoveries subagents, the inner agent has no tools: the data is
pre-computed here and embedded in the prompt, so it is one interpretive turn,
not a research loop. Matching against ``director`` as well as ``cast``
matters — auteur-type figures (writer-director-actor) are found mostly via
the director field, and for their films the whole project is one brand.
"""

import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

from agents import Agent, RunConfig, RunContextWrapper, Runner
from agents.models.openai_provider import OpenAIProvider
from decouple import config
from openai import AsyncOpenAI
from sqlalchemy import bindparam, text

from ..plex_utils import aget_plex_details
from ..redis_client import get_redis_client, redis_get_json, redis_set_json
from ..session import db_session
from .base import ToolContext
from .safe_tool import safe_tool

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
logger = logging.getLogger(__name__)

# Served locally; the gateway's only chat model (see OPENAI_BASE_URL).
MODEL = "qwen3.8"

# Loaded at import time, matching the discoveries subagents.
_SYSTEM_PROMPT = (_PROMPTS_DIR / "cast_history.md").read_text()

# The dossier goes stale when the user adds/deletes titles; 6h matches the
# discoveries and taste-signal era caches. Bump the version on prompt change.
CACHE_TTL_SECONDS = 6 * 60 * 60
CACHE_KEY_VERSION = "v2"

CAST_LIMIT = 10
TITLES_PER_PERSON_CAP = 15
PLEX_LOOKUP_CAP = 20
REPORT_CHAR_CAP = 2400

# For each person name: every same-type library title in which they appear in
# ``cast`` and/or as ``director``. The UNION dedupes a name present in both
# fields of one row; the EXISTS flags say which. Leave-one-out on uid so a
# previously added candidate doesn't "follow itself".
_CROSS_REF_SQL = """
SELECT lower(btrim(p.name)) AS person,
       i.uid,
       i.title,
       i.attributes->>'year' AS yr,
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
            {"t": r.title, "y": year, "added": bool(r.added), "role": role}
        )
        if r.added:
            block["added"] += 1
    for block in out.values():
        block["titles"].sort(key=lambda t: t["y"] or 0, reverse=True)
    return out


async def _plex_annotate(item_type: str, titles: List[Dict[str, Any]]) -> None:
    """Attach live plex status to the most signal-bearing titles (in place).

    ``in_library`` when the Plex server has the title (follow evidence even
    if never added through the curator); ``missing`` when added but gone
    (deleted — strong negative). Never-added titles get no key: absence
    carries no signal. Plex is only tracked for movies upstream. Failures
    degrade to no plex data — the dossier still works on ``added`` alone.
    """
    if item_type != "mv" or not titles:
        return

    def _priority(t: Dict[str, Any]) -> tuple[bool, int]:
        return (0 if t["added"] else 1, -(t["y"] or 0))

    picked = sorted(titles, key=_priority)[:PLEX_LOOKUP_CAP]

    async def _one(t: Dict[str, Any]) -> None:
        try:
            found = await aget_plex_details(t["t"], t["y"])
        except Exception:
            # Plex unreachable is not "deleted" — leave the plex axis empty
            # rather than fabricating a negative.
            logger.exception("cast_history plex lookup failed for %s", t["t"])
            return
        if found:
            t["plex"] = "in_library"
        elif t["added"]:
            t["plex"] = "missing"

    await asyncio.gather(*(_one(t) for t in picked))


def _user_prompt(
    item_type: str, candidate: Dict[str, Any], blocks: Dict[str, Dict[str, Any]]
) -> str:
    people: Dict[str, Any] = {}
    for name, block in sorted(blocks.items()):
        shown = block["titles"][:TITLES_PER_PERSON_CAP]
        people[name] = {
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
    }
    return json.dumps(payload, default=str)


_CAST_AGENT = Agent(
    name="cast_history",
    model=MODEL,
    instructions=_SYSTEM_PROMPT,
)


async def _fetch_dossier(cache_key: str, user_prompt: str) -> Dict[str, Any]:
    redis = get_redis_client()
    cached = redis_get_json(redis, cache_key)
    if isinstance(cached, dict) and "report" in cached:
        logger.info("cast_history cache hit key=%s", cache_key)
        return cached

    # Per-call client so the httpx transport is bound to this event loop and
    # closed before the task exits — see indexer_utils/ai_tools/agent.py.
    openai_client = AsyncOpenAI(
        api_key=config("OPENAI_API_KEY"),
        base_url=config("OPENAI_BASE_URL", default=None),
    )
    provider = OpenAIProvider(openai_client=openai_client)
    run_config = RunConfig(tracing_disabled=True, model_provider=provider)
    try:
        try:
            result = await Runner.run(
                _CAST_AGENT, user_prompt, max_turns=2, run_config=run_config
            )
        finally:
            await provider.aclose()
            await openai_client.close()
    except Exception as exc:
        logger.exception("cast_history subagent failed")
        return {"error": f"{exc.__class__.__name__}: {exc}"}

    dossier = str(result.final_output or "").strip()
    if not dossier:
        return {"error": "subagent returned empty dossier"}

    payload = {"as_of": date.today().isoformat(), "report": dossier[:REPORT_CHAR_CAP]}
    redis_set_json(redis, cache_key, payload, CACHE_TTL_SECONDS)
    return payload


@safe_tool
async def search_cast_history(
    wrapper: RunContextWrapper[ToolContext],
) -> Dict[str, Any]:
    """Full cast/director track record of the candidate against the user's library.

    Cross-references the candidate's top cast (billing order) and director
    against every same-type title in the library: per person, the titles they
    appear in with added and plex status (in_library / missing = deleted).
    A subagent turns that into a short prose dossier — per-person stats and a
    pattern label (followed across the board / selective / actively avoided /
    no evidence) plus a net pull-or-drag verdict.

    This is the deep version of the taste_signal cast_xref counts: call it
    when cast or director is the decisive lane, and let its dossier supersede
    cast_xref.

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

    blocks = await _cross_ref(ctx.item_type, names, str(candidate.get("uid") or ""))
    await _plex_annotate(
        ctx.item_type, [t for b in blocks.values() for t in b["titles"]]
    )
    user_prompt = _user_prompt(ctx.item_type, candidate, blocks)
    cache_key = (
        f"mediamanager:cast_history:{CACHE_KEY_VERSION}:{ctx.item_type}:"
        f"{candidate.get('uid')}"
    )
    return await _fetch_dossier(cache_key, user_prompt)
