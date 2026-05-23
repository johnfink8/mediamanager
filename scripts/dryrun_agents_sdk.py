#!/usr/bin/env python
"""End-to-end dry run of the openai-agents-SDK recommendation flow.

Calls the real OpenAI API. Hardcoded famous movie + show, no DB lookup —
DB / Plex / Radarr / Sonarr / Weaviate tool calls will likely error since
this hits real backends with no fixture data. The Agent loop catches tool
errors and feeds them back, so the run continues; we're after the tool_log
+ final Recommendation to verify the SDK wiring is sound.

Usage:
    ./venv/bin/python scripts/dryrun_agents_sdk.py
    ./venv/bin/python scripts/dryrun_agents_sdk.py --mv-only
    ./venv/bin/python scripts/dryrun_agents_sdk.py --tv-only

Requires OPENAI_API_KEY in env / .env.
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from decouple import config  # noqa: E402

from indexer_utils.ai_recs import (  # noqa: E402
    AGENT_MAX_TOOL_CALLS,
    AGENT_MAX_TURNS,
    OPENAI_MODEL,
    RECOMMENDATION_PROMPTS,
)
from indexer_utils.ai_tools import (  # noqa: E402
    AgentRunResult,
    ToolContext,
    run_recommendation,
)

# Movie: an unreleased mid-tier theatrical with a genuinely open reception
# question. Video-game adaptations are historically risky, the first MK
# (2021) was middling, the sequel reworks the cast — there's no "obvious
# tentpole, recommend on brand alone" shortcut here, which is what we want:
# the agent should feel the need to fire search_title_buzz for community
# chatter / pre-release press, not just check the box-office chart.
# release_count=0, no ratings = no prior theatrical record. Read-only.
MOVIE_CANDIDATE = {
    "uid": "tt_mortal_kombat_2_2026",
    "title": "Mortal Kombat II",
    "year": 2026,
    "genres": ["Action", "Fantasy"],
    "language": ["en"],
    "director": "Simon McQuoid",
    "studio": "New Line Cinema / Warner Bros.",
    "network": None,
    "runtime": None,
    "rating_value": None,
    "rating_votes": None,
    "release_count": 0,
    "cast": [
        "Karl Urban",
        "Lewis Tan",
        "Hiroyuki Sanada",
        "Tati Gabrielle",
        "Adeline Rudolph",
    ],
    "synopsis": (
        "Sequel to the 2021 Mortal Kombat reboot. Earthrealm's fighters "
        "regroup ahead of the tournament foretold in the first film, with "
        "Karl Urban joining the cast as Johnny Cage."
    ),
}

SHOW_CANDIDATE = {
    "uid": "81189",
    "title": "Breaking Bad",
    "year": 2008,
    "genres": ["Crime", "Drama", "Thriller"],
    "language": ["en"],
    "director": None,
    "studio": None,
    "network": "AMC",
    "runtime": 49,
    "rating_value": 9.5,
    "rating_votes": 2_000_000,
    "cast": ["Bryan Cranston", "Aaron Paul", "Anna Gunn"],
    "synopsis": (
        "A terminally ill chemistry teacher partners with a former student to "
        "manufacture methamphetamine, sliding from anxious provider to "
        "ruthless drug kingpin."
    ),
}


def _print_result(
    label: str, candidate: Dict[str, Any], result: AgentRunResult
) -> None:
    print()
    print("=" * 72)
    print(f"{label}: {candidate['title']} ({candidate['year']})")
    print(f"Turns: {result.turns}   Tool calls: {result.tool_calls}")
    print("-" * 72)
    if result.tool_log:
        for entry in result.tool_log:
            preview = entry.get("output_preview") or ""
            if len(preview) > 120:
                preview = preview[:120] + " …"
            print(f"  · {entry['name']:<28} {entry['duration_ms']:>5}ms  {preview}")
    else:
        print("  (no tool calls)")
    print("-" * 72)
    if result.submission is not None:
        print("Submission:", json.dumps(result.submission, indent=2, default=str))
    if result.failure is not None:
        print("Failure:   ", json.dumps(result.failure, indent=2, default=str))


async def run_one(item_type: str, candidate: Dict[str, Any]) -> None:
    system_prompt = RECOMMENDATION_PROMPTS[item_type]
    user_prompt = json.dumps(
        {"item_type": item_type, "candidate": candidate}, default=str
    )
    ctx = ToolContext(
        item_type=item_type,
        candidate={
            "uid": candidate["uid"],
            "title": candidate["title"],
            "year": candidate["year"],
            "genres": candidate["genres"],
        },
    )
    result = await run_recommendation(
        item_type=item_type,
        model=OPENAI_MODEL,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        ctx=ctx,
        max_turns=AGENT_MAX_TURNS,
        max_tool_calls=AGENT_MAX_TOOL_CALLS,
    )
    label = "MOVIE" if item_type == "mv" else "SHOW"
    _print_result(label, candidate, result)


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mv-only", action="store_true", help="Skip the TV run.")
    ap.add_argument("--tv-only", action="store_true", help="Skip the movie run.")
    args = ap.parse_args()

    if not config("OPENAI_API_KEY", default=""):
        print("OPENAI_API_KEY not set — refusing to run a fake dry-run.")
        sys.exit(2)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s  %(message)s",
    )
    print(f"Model: {OPENAI_MODEL}")
    print(f"max_turns={AGENT_MAX_TURNS}  max_tool_calls={AGENT_MAX_TOOL_CALLS}")

    if not args.tv_only:
        await run_one("mv", MOVIE_CANDIDATE)
    if not args.mv_only:
        await run_one("tv", SHOW_CANDIDATE)


if __name__ == "__main__":
    asyncio.run(main())
