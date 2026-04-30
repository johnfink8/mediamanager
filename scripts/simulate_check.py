#!/usr/bin/env python
"""Run ``check_movies`` / ``check_shows`` against fixtures + real OpenAI.

Usage:
    # First runs (cheap): one fixture item only
    ./venv/bin/python scripts/simulate_check.py movies --max-items 1
    ./venv/bin/python scripts/simulate_check.py shows --max-items 1

    # Full fixture set
    ./venv/bin/python scripts/simulate_check.py both

Connects to the real local MySQL (read-only — writes are intercepted), the
real local Weaviate (read-only — upserts are intercepted), and the real
OpenAI API. Indexer / Radarr / Sonarr / Plex / TMDB are mocked from
``indexer_utils.testing.fixtures``.

Pre-flight:
    docker compose up -d db weaviate         # bring up backing services
    export WEAVIATE_HOST=localhost           # docker-compose maps 8080
    export OPENAI_API_KEY=...                # required, real calls

If Weaviate is unreachable the ``search_similar_by_synopsis`` tool will
return an error and the agent will pivot to ``search_by_genre`` /
``get_item_details`` — the simulation still completes.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from indexer_utils.testing import SimulationRecorder, simulate_environment  # noqa: E402


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    # Wipe any prior handlers so we don't double-print.
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    # Quiet a few chatty libraries.
    for name in ("httpx", "httpcore", "openai._base_client"):
        logging.getLogger(name).setLevel(logging.WARNING)


def _truncate(text: Any, n: int = 240) -> str:
    s = str(text)
    return s if len(s) <= n else s[:n] + f"... <+{len(s) - n}>"


def _print_section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


def _print_candidate_summary(item: Dict[str, Any]) -> None:
    title = item.get("title")
    uid = item.get("uid")
    item_type = item.get("item_type")
    attrs = item.get("attributes") or {}
    ai = attrs.get("ai") or {}

    print()
    print(f"--- {item_type}:{uid} {title}")
    print(
        f"  verdict: recommend={ai.get('value')} "
        f"score={ai.get('score')} "
        f"failed={ai.get('failed')}"
    )
    if ai.get("reason"):
        print(f"  reason : {_truncate(ai['reason'])}")
    if ai.get("synopsis"):
        print(f"  synopsis: {_truncate(ai['synopsis'], 320)}")
    if ai.get("failure"):
        print(f"  failure: {ai['failure']}")
    print(f"  turns={ai.get('turns')} tool_calls={ai.get('tool_calls')}")

    tool_log: List[Dict[str, Any]] = ai.get("tool_log") or []
    for i, entry in enumerate(tool_log, 1):
        err = entry.get("error") or "-"
        ms = entry.get("duration_ms")
        args_preview = _truncate(json.dumps(entry.get("arguments") or {}), 160)
        print(
            f"  [{i:02d}] {entry.get('name')}  ms={ms}  err={err}  args={args_preview}"
        )
        out = entry.get("output_preview")
        if out:
            print(f"       => {_truncate(out, 320)}")


def _print_recorder(recorder: SimulationRecorder) -> None:
    _print_section(f"SIMULATION RESULTS  ({len(recorder.created_items)} candidate(s))")
    for item in recorder.created_items:
        _print_candidate_summary(item)

    _print_section("EXTERNAL CALLS RECORDED")
    print(f"indexer_requests : {len(recorder.indexer_requests)}")
    print(f"radarr_calls     : {len(recorder.radarr_calls)}")
    print(f"sonarr_calls     : {len(recorder.sonarr_calls)}")
    print(f"plex_lookups     : {len(recorder.plex_lookups)}")
    print(f"weaviate upserts : {len(recorder.upsert_calls)} (intercepted, not written)")
    print(f"sqlite seed      : {recorder.seed_summary or '-'}")
    print(f"weaviate seed    : {recorder.weaviate_seed_summary or '-'}")
    if recorder.weaviate_error:
        print(f"weaviate error   : {recorder.weaviate_error}")


def _run(kind: str, max_items: int) -> SimulationRecorder:
    from indexer_utils import vid_utils

    with simulate_environment(max_feed_items=max_items or None) as recorder:
        if kind == "movies":
            vid_utils.check_movies(days=7)
        elif kind == "shows":
            vid_utils.check_shows(days=7)
        else:
            raise ValueError(f"unknown kind: {kind}")
    return recorder


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("movies", "shows", "both"))
    parser.add_argument(
        "--max-items",
        type=int,
        default=0,
        help=(
            "Cap how many feed items get processed (0 = use the full fixture). "
            "Each item costs OpenAI tokens, so prefer 1 for first runs."
        ),
    )
    parser.add_argument("--quiet", action="store_true", help="Reduce log verbosity.")
    parser.add_argument(
        "--debug", action="store_true", help="Enable DEBUG logging on the agent loop."
    )
    args = parser.parse_args(argv)

    _configure_logging(verbose=args.debug and not args.quiet)
    if args.quiet:
        logging.getLogger().setLevel(logging.WARNING)
        # Keep the agent loop visible — that's the whole point.
        logging.getLogger("indexer_utils.ai_tools.loop").setLevel(logging.INFO)

    kinds = ("movies", "shows") if args.kind == "both" else (args.kind,)
    for kind in kinds:
        _print_section(f"RUN: check_{kind}")
        recorder = _run(kind, args.max_items)
        _print_recorder(recorder)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
