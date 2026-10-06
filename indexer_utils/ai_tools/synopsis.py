"""Synopsis research agent: a short, accurate description of one candidate.

The synopsis is shown under the title and, more importantly, embedded as
the candidate's ``synopsis_vector``, which drives every similarity signal
the recommendation agent sees. It used to be written from the model's
memory given only the title, genres and cast, so a new release got an
invented plot. Now the agent starts from what TMDB actually says (the
distributor's overview, franchise, networks) and researches the rest on
the web, and is told to say "plot details aren't public" rather than
guess.
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents import Agent

from ..tmdb import get_title_details
from .research import MODEL, ResearchRun, ResearchSpec, run_research
from .webtools import WEB_TOOLS

logger = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"
# Usually 1-3 turns: read the overview, maybe check franchise status.
SYNOPSIS_MAX_TURNS = 6
SYNOPSIS_CLIP = 600
SYNOPSIS_CAST = 6


# The reply is prose, not a structured ``output_type``: with a JSON schema
# attached, vLLM constrains every turn to that schema, so the model can
# never emit a tool call and answers from memory on the first turn.
_SOURCES_LINE = re.compile(r"^\W*sources\W*$", re.IGNORECASE)
_NOT_SYNOPSIS = re.compile(r"^\(.*\)$|:$|^#")


def parse_reply(text: str) -> tuple[str, List[str]]:
    """Split the agent's reply into (synopsis, sources).

    The synopsis is the last real paragraph before the ``Sources:`` line —
    the model sometimes leads with a heading or a "drafted synopsis:" note,
    or trails a character count, however firmly the prompt says not to.
    """
    lines = text.strip().splitlines()
    cut = next((i for i, ln in enumerate(lines) if _SOURCES_LINE.match(ln)), None)
    body = lines if cut is None else lines[:cut]
    tail = [] if cut is None else lines[cut + 1 :]
    paragraphs = [
        " ".join(p.split()).strip("*_ ")
        for p in "\n".join(body).split("\n\n")
        if p.strip()
    ]
    real = [p for p in paragraphs if not _NOT_SYNOPSIS.search(p)]
    synopsis = real[-1] if real else ""
    sources = []
    for ln in tail:
        src = ln.strip().lstrip("-*• ").strip()
        # "https://… (what it gave)" — keep the URL, drop the note.
        src = src.split(" (", 1)[0].strip("<>`") if src.startswith("http") else src
        if src:
            sources.append(src)
    return synopsis, sources


SYNOPSIS = ResearchSpec(
    name="synopsis",
    agent=Agent(
        name="synopsis",
        model=MODEL,
        instructions=(_PROMPTS_DIR / "synopsis.md").read_text(),
        tools=WEB_TOOLS,
    ),
    max_turns=SYNOPSIS_MAX_TURNS,
)


async def tmdb_details(item_type: str, tmdb_id: Any) -> Optional[Dict[str, Any]]:
    """``get_title_details``, or None when the id is unknown or TMDB fails."""
    if not tmdb_id:
        return None
    try:
        return await asyncio.to_thread(get_title_details, item_type, int(tmdb_id))
    except Exception:
        logger.exception("tmdb details failed for %s:%s", item_type, tmdb_id)
        return None


def synopsis_input(
    item_type: str, candidate: Dict[str, Any], tmdb: Optional[Dict[str, Any]]
) -> str:
    """The agent's user prompt: every fact we hold about the work, as JSON."""
    uid = str(candidate.get("uid") or "")
    ids: Dict[str, Any] = {}
    if item_type == "mv" and uid.startswith("tt"):
        ids["imdb"] = uid
    elif item_type == "tv" and uid:
        ids["tvdb"] = uid
    if candidate.get("tmdb_id"):
        ids["tmdb"] = candidate["tmdb_id"]
    facts: Dict[str, Any] = {
        "item_type": "movie" if item_type == "mv" else "tv series",
        "title": (tmdb or {}).get("title") or candidate.get("title"),
        "year": candidate.get("year"),
        "ids": ids,
        "director": candidate.get("director"),
        "cast": list(candidate.get("cast") or [])[:SYNOPSIS_CAST],
        "genres": candidate.get("genres"),
        "language": candidate.get("language"),
        "network": candidate.get("network"),
        "tmdb": tmdb,
    }
    if not tmdb:
        facts["tmdb"] = "no TMDB record found — research the basics yourself"
    return json.dumps(
        {k: v for k, v in facts.items() if v not in (None, "", [], {})},
        ensure_ascii=False,
    )


TMDB_SOURCE = "TMDB overview"


# ``web_fetch`` returns ``{'url': …, 'chars': N, …}`` for a page and
# ``{'error': …}`` for a blocked URL, HTTP error or timeout.
_FETCHED_CHARS = re.compile(r"""^\{['"]url['"].*?['"]chars['"]: (\d+)""")


def fetched_urls(tool_log: List[Dict[str, Any]]) -> List[str]:
    """Every URL the run fetched with ``web_fetch`` and got content back from."""
    urls = []
    for call in tool_log:
        if call.get("name") != "web_fetch":
            continue
        got = _FETCHED_CHARS.match(str(call.get("output_preview") or ""))
        if not got or not int(got.group(1)):
            continue
        try:
            url = json.loads(call.get("arguments") or "{}").get("url")
        except (TypeError, ValueError, AttributeError):
            continue
        if url:
            urls.append(str(url).strip())
    return urls


def verify_sources(
    sources: List[str], tool_log: List[Dict[str, Any]], *, had_overview: bool
) -> tuple[List[str], List[str]]:
    """Split cited ``sources`` into (kept, dropped).

    The model cites pages it never opened, and even constructs URLs from
    the IDs it was given, so a source is kept only if it is the TMDB
    overview it was handed (``had_overview``) or a URL it actually fetched
    in this run.
    """
    fetched = {u.rstrip("/") for u in fetched_urls(tool_log)}
    kept, dropped = [], []
    for src in sources:
        s = str(src).strip()
        is_overview = had_overview and s.lower() == TMDB_SOURCE.lower()
        if is_overview or s.rstrip("/") in fetched:
            kept.append(s)
        else:
            dropped.append(s)
    return kept, dropped


@dataclass
class SynopsisResult:
    synopsis: Optional[str]
    sources: List[str]
    failure: Optional[Dict[str, Any]]
    run: ResearchRun


async def research_synopsis(
    item_type: str,
    candidate: Dict[str, Any],
    tmdb: Optional[Dict[str, Any]],
    *,
    max_turns: Optional[int] = None,
) -> SynopsisResult:
    """Run the synopsis agent; a failure comes back as ``failure``, never raised."""
    run = await run_research(
        SYNOPSIS,
        synopsis_input(item_type, candidate, tmdb),
        max_turns=max_turns,
        log_tag=f"synopsis[{item_type}:{candidate.get('uid')}]",
    )
    text, cited = parse_reply(str(run.output or ""))
    if not text:
        code = "research_failed" if run.error else "missing_synopsis"
        return SynopsisResult(
            synopsis=None,
            sources=[],
            failure={
                "code": code,
                "message": run.error or "Synopsis missing from AI response",
                "stage": "synopsis",
                "step": "synopsis",
            },
            run=run,
        )
    sources, dropped = verify_sources(
        cited, run.tool_log, had_overview=bool((tmdb or {}).get("overview"))
    )
    if dropped:
        logger.warning(
            "synopsis[%s:%s] cited pages it never fetched: %s",
            item_type,
            candidate.get("uid"),
            dropped,
        )
    return SynopsisResult(
        synopsis=text[:SYNOPSIS_CLIP],
        sources=sources,
        failure=None,
        run=run,
    )
