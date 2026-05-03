#!/usr/bin/env python
"""Invoke ``search_recent_tv`` and dump the subagent's prose dossier.
Mirror of ``explore_search_recent_releases.py`` but for the TV variant.

Usage:
    ./venv/bin/python scripts/explore_search_recent_tv.py
    ./venv/bin/python scripts/explore_search_recent_tv.py --weeks-back 3 --weeks-forward 3
    ./venv/bin/python scripts/explore_search_recent_tv.py --top-n 10 --focus "limited series"

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
from indexer_utils.ai_tools.discoveries import (  # noqa: E402
    SEARCH_RECENT_TV_TOOL,
)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--weeks-back", type=int, default=2)
    ap.add_argument("--weeks-forward", type=int, default=2)
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--focus", type=str, default="")
    args = ap.parse_args()

    ctx = ToolContext(item_type="tv", candidate={"uid": "explore"})
    tool_input = {
        "weeks_back": args.weeks_back,
        "weeks_forward": args.weeks_forward,
        "top_n": args.top_n,
        "focus": args.focus,
    }

    result = await SEARCH_RECENT_TV_TOOL.execute(tool_input, ctx)
    print(json.dumps(result.output, indent=2, default=str))
    print("\n\n=== PROSE DOSSIER ===\n\n")
    print(result.output.get("report", ""))


if __name__ == "__main__":
    asyncio.run(main())
