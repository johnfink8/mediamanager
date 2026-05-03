#!/usr/bin/env python
"""Invoke ``check_added_history`` against the live DB + arr/Plex APIs and
dump the raw output. For exploring whether the structured payload is rich
enough for the recommender, or whether an LLM-summarization layer would buy
something.

Usage:
    ./venv/bin/python scripts/explore_check_added_history.py
    ./venv/bin/python scripts/explore_check_added_history.py --type mv --limit 25
    ./venv/bin/python scripts/explore_check_added_history.py --days-back 90

Hits the real local MySQL, real Radarr, real Sonarr, real Plex. No OpenAI.
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
from indexer_utils.ai_tools.inspections import CHECK_ADDED_HISTORY_TOOL  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--type", choices=["mv", "tv", "any"], default="any")
    ap.add_argument("--limit", type=int, default=15)
    ap.add_argument("--days-back", type=int, default=None)
    args = ap.parse_args()

    # The tool reads ctx.item_type when input.item_type is omitted, and
    # ctx.candidate["uid"] only for budget-clipping log lines.
    ctx = ToolContext(item_type="mv", candidate={"uid": "explore"})
    tool_input = {"limit": args.limit, "item_type": args.type}
    if args.days_back is not None:
        tool_input["days_back"] = args.days_back

    result = await CHECK_ADDED_HISTORY_TOOL.execute(tool_input, ctx)
    print(json.dumps(result.output, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
