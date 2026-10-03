#!/usr/bin/env python3
"""Run one research subagent live, outside the recommendation flow.

Every research agent (synopsis, title buzz, cast history, the two
release-window reports) runs here exactly as it does in production: the
same prompt builders, fed the same real inputs (the candidate's catalog row,
hydrated from TMDB, with the catalog and Plex behind cast history), with
real web tools. Only the Redis dossier caches are skipped, so every run
does the work. Nothing is written to the DB.

Each run prints its tool calls as they happen, then the output and a
summary, and saves a JSON record (input, output, tool log with arguments)
to ``--out`` for auditing.

Usage:
    python research_harness.py synopsis --item-type mv --uid tt6933238
    python research_harness.py buzz --item-type tv --uid 242521
    python research_harness.py cast --item-type mv --uid tt31349844
    python research_harness.py recent-releases
    python research_harness.py recent-tv --weeks-back 1
    python research_harness.py synopsis --cases          # the curated set
    python research_harness.py cast --uid tt0039439 --dry-run   # input only

Run it where the app's network is: inside the app container, or in a
throwaway one from the app image with this tree mounted:
    docker run --rm --network container:servermonitor-servermonitor-1 \\
        -v "$PWD":/opt/servermonitor servermonitor-servermonitor \\
        python research_harness.py synopsis --cases
"""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from sqlalchemy import select

from indexer_utils.ai_recs import candidate_context, hydrate_candidate
from indexer_utils.ai_tools import cast_history, discoveries
from indexer_utils.ai_tools.base import ToolContext
from indexer_utils.ai_tools.research import ResearchRun, ResearchSpec, run_research
from indexer_utils.ai_tools.synopsis import (
    SYNOPSIS,
    parse_reply,
    synopsis_input,
    verify_sources,
)
from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session

# Real catalog rows chosen to cover the hard cases: brand-new releases with
# little written about them, sequels, a title shared with later remakes, a
# revival of an older series, and non-English TV.
CASES: List[Dict[str, str]] = [
    {"item_type": "mv", "uid": "tt6933238", "why": "new 2026 release"},
    {"item_type": "mv", "uid": "tt40142495", "why": "obscure 2026 sequel"},
    {"item_type": "mv", "uid": "tt39396063", "why": "micro-budget 2026 horror"},
    {"item_type": "mv", "uid": "tt31349844", "why": "wide 2026 release, added"},
    {"item_type": "mv", "uid": "tt0039439", "why": "1947 film; 2018/2021 remakes"},
    {"item_type": "tv", "uid": "242521", "why": "2012 revival of Dallas"},
    {"item_type": "tv", "uid": "473998", "why": "new 2026 spin-off series"},
    {"item_type": "tv", "uid": "466383", "why": "Hindi-language series"},
    {"item_type": "tv", "uid": "78544", "why": "1975 series, remade 2008"},
]


@dataclass
class Prepared:
    """A spec with its user prompt built, ready to run."""

    label: str
    spec: ResearchSpec
    user_prompt: str
    context: Any = None
    candidate: Optional[Dict[str, Any]] = None

    def candidate_label(self) -> str:
        c = self.candidate
        if not c:
            return "no candidate"
        return f"{c['title']} ({c.get('year')}) [{c['uid']}]"


async def load_candidate(item_type: str, uid: str) -> Dict[str, Any]:
    """The candidate as production builds it: catalog row, hydrated from TMDB.

    Hydration works on a copy of the row's attributes; nothing is saved.
    Returns the ``candidate_context`` plus ``_tmdb`` (TMDB details).
    """
    async with db_session() as session:
        item = (
            await session.execute(
                select(IgnoreItem)
                .where(IgnoreItem.item_type == item_type, IgnoreItem.uid == uid)
                .order_by(IgnoreItem.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if item is None:
            raise SystemExit(f"no {item_type} row with uid {uid}")
        title, attrs = item.title, dict(item.attributes or {})
    tmdb = await hydrate_candidate(item_type, uid, attrs)
    candidate = candidate_context(
        item_type, uid, attrs.get("tmdb_title") or title, attrs
    )
    return {**candidate, "_tmdb": tmdb}


def _today() -> Any:
    return datetime.now(discoveries._TODAY_TZ).date()


async def prep_synopsis(args: argparse.Namespace, c: Dict[str, Any]) -> Prepared:
    it = args.item_type
    return Prepared("synopsis", SYNOPSIS, synopsis_input(it, c, c["_tmdb"]))


async def prep_buzz(args: argparse.Namespace, c: Dict[str, Any]) -> Prepared:
    it = args.item_type
    title = args.title or c["title"]
    year = args.year or (c.get("year") if not args.title else None)
    prompt = discoveries._build_buzz_prompt(
        today=_today(),
        title=title,
        year=year,
        item_type=it,
        known=discoveries.candidate_facts(it, c, title),
    )
    return Prepared("buzz", discoveries.TITLE_BUZZ, prompt)


async def prep_cast(args: argparse.Namespace, c: Dict[str, Any]) -> Prepared:
    it = args.item_type
    if not cast_history._person_names(c):
        raise SystemExit(f"{c['uid']} has no cast or director to research")
    prompt = await cast_history.build_cast_input(it, c)
    ctx = ToolContext(item_type=it, candidate=c)
    return Prepared("cast", cast_history.CAST_HISTORY, prompt, context=ctx)


def _window(args: argparse.Namespace) -> Dict[str, Any]:
    return {
        "today": _today(),
        "weeks_back": args.weeks_back,
        "weeks_forward": args.weeks_forward,
        "top_n": args.top_n,
        "focus": args.focus,
    }


async def prep_releases(args: argparse.Namespace, _: Any) -> Prepared:
    prompt = discoveries._build_movies_prompt(**_window(args))
    return Prepared("recent-releases", discoveries.RECENT_RELEASES, prompt)


async def prep_tv(args: argparse.Namespace, _: Any) -> Prepared:
    prompt = discoveries._build_tv_prompt(**_window(args))
    return Prepared("recent-tv", discoveries.RECENT_TV, prompt)


Prep = Callable[[argparse.Namespace, Any], Awaitable[Prepared]]
# name -> (prompt builder, needs a candidate)
SPECS: Dict[str, tuple[Prep, bool]] = {
    "synopsis": (prep_synopsis, True),
    "buzz": (prep_buzz, True),
    "cast": (prep_cast, True),
    "recent-releases": (prep_releases, False),
    "recent-tv": (prep_tv, False),
}


def _short(value: Any, n: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _output_text(run: ResearchRun) -> str:
    return str(run.output or "")


def report(prep: Prepared, run: ResearchRun, record_path: Path) -> None:
    print(f"\n=== {prep.label} · {prep.candidate_label()} ===")
    for i, call in enumerate(run.tool_log, 1):
        print(
            f"  {i:>2}. {call['name']}({_short(call.get('arguments') or '', 90)})"
            f" {call.get('duration_ms')}ms -> {_short(call['output_preview'], 110)}"
        )
    if run.error:
        print(f"\nERROR: {run.error}")
    else:
        print("\n" + _output_text(run))
    if prep.spec is SYNOPSIS and run.output:
        synopsis, cited = parse_reply(str(run.output))
        tmdb = (prep.candidate or {}).get("_tmdb") or {}
        kept, dropped = verify_sources(
            cited, run.tool_log, had_overview=bool(tmdb.get("overview"))
        )
        print(f"\nPARSED ({len(synopsis)} chars): {synopsis}\nSOURCES: {kept}")
        if dropped:
            print(f"UNFETCHED SOURCES (dropped in production): {dropped}")
    print(
        f"\nturns={run.turns}/{prep.spec.max_turns} tool_calls={run.tool_calls} "
        f"elapsed={run.elapsed_s:.0f}s  record: {record_path}"
    )


async def run_one(
    args: argparse.Namespace, item_type: Optional[str], uid: Optional[str]
) -> ResearchRun:
    prep_fn, needs_candidate = SPECS[args.spec]
    candidate = None
    if needs_candidate:
        if not (item_type and uid):
            raise SystemExit(f"{args.spec} needs --item-type and --uid (or --cases)")
        candidate = await load_candidate(item_type, uid)
        args = argparse.Namespace(**{**vars(args), "item_type": item_type})
    prep = await prep_fn(args, candidate)
    prep.candidate = candidate

    if args.dry_run or args.show_input:
        print(f"--- {prep.label} input · {prep.candidate_label()} ---")
        print(prep.user_prompt)
        if args.dry_run:
            return ResearchRun()

    run = await run_research(
        prep.spec,
        prep.user_prompt,
        context=prep.context,
        max_turns=args.turns,
        log_tag=f"{prep.label}[{uid or '-'}]",
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"{stamp}_{prep.label}_{item_type or 'x'}_{uid or 'window'}.json"
    record = {
        "spec": prep.label,
        "agent": prep.spec.name,
        "max_turns": args.turns or prep.spec.max_turns,
        "candidate": {k: v for k, v in (candidate or {}).items() if k != "_tmdb"},
        "input": prep.user_prompt,
        "output": run.output,
        "error": run.error,
        **run.audit(),
    }
    path.write_text(json.dumps(record, indent=2, default=str, ensure_ascii=False))
    report(prep, run, path)
    return run


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("spec", choices=sorted(SPECS))
    ap.add_argument("--item-type", choices=["mv", "tv"])
    ap.add_argument("--uid", help="catalog uid (IMDb id for movies, TVDB for TV)")
    ap.add_argument(
        "--cases", action="store_true", help="run the curated CASES instead"
    )
    ap.add_argument("--parallel", type=int, default=2, help="concurrent cases")
    ap.add_argument("--turns", type=int, help="override the spec's turn budget")
    ap.add_argument("--dry-run", action="store_true", help="print the input only")
    ap.add_argument("--show-input", action="store_true")
    ap.add_argument("--out", default="research_runs", help="record directory")
    ap.add_argument("--title", help="buzz: research this title instead")
    ap.add_argument("--year", type=int, help="buzz: year for --title")
    ap.add_argument("--weeks-back", type=int, default=2)
    ap.add_argument("--weeks-forward", type=int, default=2)
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--focus")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(name)s %(message)s"
    )
    # The audit hooks print each turn and tool call live on their own
    # handler; stop them printing twice through the root one.
    logging.getLogger("indexer_utils.ai_tools.hooks").propagate = False

    if not args.cases:
        run = await run_one(args, args.item_type, args.uid)
        sys.exit(1 if run.error else 0)

    if not SPECS[args.spec][1]:
        raise SystemExit(f"{args.spec} takes no candidate; run it without --cases")
    sem = asyncio.Semaphore(max(1, args.parallel))

    async def one(case: Dict[str, str]) -> ResearchRun:
        async with sem:
            return await run_one(args, case["item_type"], case["uid"])

    runs = await asyncio.gather(*(one(c) for c in CASES))
    failed = sum(1 for r in runs if r.error)
    print(f"\n{len(runs) - failed}/{len(runs)} cases produced output")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    asyncio.run(main())
