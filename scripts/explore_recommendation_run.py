#!/usr/bin/env python
"""Run a single end-to-end recommendation against the agent loop with a
candidate that's a current theatrical release. Verifies whether the new
``search_recent_releases`` tool gets picked when the candidate matches
its calling criteria.

Usage:
    ./venv/bin/python scripts/explore_recommendation_run.py
    ./venv/bin/python scripts/explore_recommendation_run.py --title "Mortal Kombat II"

No harness — DB / Plex / Radarr / Sonarr / Weaviate calls will likely
error since this hits real backends. The agent loop catches tool errors
and feeds them back as JSON, so the run continues. We're after the
tool_log to see what the model picked.
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from decouple import config  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from indexer_utils.ai_recs import (  # noqa: E402
    AGENT_MAX_TOOL_CALLS,
    AGENT_MAX_TURNS,
    OPENAI_MODEL,
    RECOMMENDATION_PROMPTS,
)
from indexer_utils.ai_tools import REGISTRY, ToolContext, run_agent  # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", default="Michael")
    ap.add_argument("--uid", default="tt_michael_2026_explore")
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--genre", default="Biography")
    ap.add_argument("--studio", default="Lionsgate")
    ap.add_argument(
        "--synopsis",
        default=(
            "Biographical drama charting Michael Jackson's rise to global "
            "superstardom, from his Jackson 5 childhood through the making "
            "of Thriller, with the costs of that fame in the foreground."
        ),
    )
    args = ap.parse_args()

    candidate = {
        "title": args.title,
        "uid": args.uid,
        "year": args.year,
        "genres": [args.genre],
        "language": ["en"],
        "synopsis": args.synopsis,
        "cast": None,
        "director": None,
        "network": None,
        "studio": args.studio,
        "runtime": 120,
        "rating_value": None,
        "rating_votes": None,
        "release_count": 1,
    }
    user_payload = {"item_type": "mv", "candidate": candidate}
    system_prompt = RECOMMENDATION_PROMPTS["mv"]
    user_prompt = json.dumps(user_payload, default=str)

    ctx = ToolContext(
        item_type="mv",
        candidate={
            "uid": candidate["uid"],
            "title": candidate["title"],
            "year": candidate["year"],
            "genres": candidate["genres"],
        },
    )

    client = AsyncOpenAI(api_key=config("OPENAI_API_KEY"))
    try:
        result = await run_agent(
            client=client,
            model=OPENAI_MODEL,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            registry=REGISTRY,
            ctx=ctx,
            max_turns=AGENT_MAX_TURNS,
            max_tool_calls=AGENT_MAX_TOOL_CALLS,
        )
    finally:
        await client.close()

    print()
    print("=" * 72)
    print(f"Candidate: {candidate['title']} ({candidate['year']}, {args.studio})")
    print(f"Tool calls: {len(result.tool_log)}, turns: {result.turns}")
    print("-" * 72)
    new_tool_called = False
    for entry in result.tool_log:
        marker = ""
        if entry["name"] == "search_recent_releases":
            marker = "  ← NEW TOOL"
            new_tool_called = True
        err = f" ERROR={entry['error']}" if entry["error"] else ""
        print(f"  · {entry['name']:<28} {entry['duration_ms']:>5}ms{err}{marker}")
    print("-" * 72)
    print(
        "search_recent_releases called:",
        "YES" if new_tool_called else "NO",
    )
    print()
    print("Submission:", json.dumps(result.submission, indent=2, default=str))
    if result.failure:
        print("Failure:", json.dumps(result.failure, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
