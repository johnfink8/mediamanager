#!/usr/bin/env python
"""Invoke ``search_title_buzz`` on a single title and dump the dossier.

Usage:
    ./venv/bin/python scripts/explore_search_title_buzz.py --title "Michael" --year 2026 --item-type mv
    ./venv/bin/python scripts/explore_search_title_buzz.py --title "Severance" --year 2022 --item-type tv

Hits the OpenAI Responses API with the hosted ``web_search`` tool plus
Redis (cache). No DB, no Plex, no Radarr/Sonarr.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from indexer_utils.ai_tools.base import ToolContext  # noqa: E402
from indexer_utils.ai_tools.discoveries import SEARCH_TITLE_BUZZ_TOOL  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--year", type=int, default=None)
    ap.add_argument("--item-type", choices=["mv", "tv"], default="mv")
    args = ap.parse_args()

    ctx = ToolContext(
        item_type=args.item_type,
        candidate={"uid": "explore", "title": args.title},
    )
    tool_input = {"title": args.title, "item_type": args.item_type}
    if args.year is not None:
        tool_input["year"] = args.year

    result = await SEARCH_TITLE_BUZZ_TOOL.execute(tool_input, ctx)
    print(json.dumps(result.output, indent=2, default=str))
    print("\n\n=== PROSE DOSSIER ===\n\n")
    print(result.output.get("report", ""))


if __name__ == "__main__":
    asyncio.run(main())
