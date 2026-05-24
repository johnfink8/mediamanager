#!/usr/bin/env python3
"""Backfill ``synopsis_vector`` on ``indexer_utils_ignoreitem``.

Walks items that don't yet have a vector (or all items with ``--reindex-all``),
embeds each item's ``attributes["synopsis"]``, and writes the result into
pgvector via ``vector_search.upsert_item_vector``. Items missing a synopsis get
one generated via ``gpt-5.5`` first — unless ``--require-synopsis`` is set, in
which case they're skipped (embed-only, no generation).
"""

import argparse
import asyncio
import json
import logging
from datetime import datetime
from typing import List, Optional

from decouple import config
from sqlalchemy import select, text
from sqlalchemy.orm.attributes import flag_modified

from indexer_utils.ai_recs import agenerate_synopsis_for_candidate
from indexer_utils.filters import should_ignore_by_rules
from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session
from indexer_utils.vector_search import upsert_item_vector

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())


def _to_list_of_str(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, dict):
        return [str(v) for v in value.values()]
    return [str(value)]


def _year_from_attrs(attrs) -> Optional[int]:
    try:
        y = attrs.get("year")
        if isinstance(y, list) and y:
            return int(str(y[0]))
        if isinstance(y, (str, int)):
            return int(str(y))
    except Exception:
        return None
    return None


async def backfill(
    item_type: str,
    limit: int,
    dry_run: bool,
    force: bool,
    reindex_all: bool,
    check_vectors: bool,
    query_rules: bool,
    since: int,
    added_only: bool,
    require_synopsis: bool,
) -> None:
    if not config("OPENAI_API_KEY", default=None):
        print(
            "WARNING: OPENAI_API_KEY is not set; backfill will not generate any data."
        )
        return

    async with db_session() as session:
        stmt = select(IgnoreItem).where(IgnoreItem.item_type == item_type)
        if added_only:
            stmt = stmt.where(IgnoreItem.added.is_(True))
            logger.info("Restricting to added=True items")
        if require_synopsis:
            # ``->>`` (``.astext``) so a JSON-null synopsis counts as absent.
            stmt = stmt.where(
                IgnoreItem.attributes["synopsis"].astext.isnot(None)
                | IgnoreItem.attributes["ai"]["synopsis"].is_not(None)
            )
            logger.info(
                "Restricting to items with a stored synopsis (embed only, no generation)"
            )
        logger.info(f"Querying {item_type} items")

        # FilterRule disqualification is enforced per-item below via
        # ``should_ignore_by_rules`` — the same check the ingest pipeline uses.
        # There's no SQL pre-filter: the candidate set is already small, and the
        # inline translation that used to live here emitted JSONB ``@>`` against
        # bare strings (and had drifted out of sync with the operator names in
        # ``indexer_utils.filters``).

        if since:
            # JSONB ``year`` is mostly an int but legacy rows have stored lists
            # (``[2022]``) and the occasional raw string. Gate the ``::int``
            # cast behind a regex so non-numeric values silently fall out
            # instead of aborting the whole backfill.
            stmt = stmt.where(
                text(
                    "(CASE WHEN (attributes->>'year') ~ '^-?[0-9]+$' "
                    "THEN (attributes->>'year')::int END) >= :since_year"
                ).bindparams(since_year=since)
            )

        if check_vectors:
            logger.info("Checking items that already have a synopsis vector")
            stmt = stmt.where(IgnoreItem.synopsis_vector.is_not(None))
        elif not reindex_all:
            logger.info("Excluding items that already have a synopsis vector")
            stmt = stmt.where(IgnoreItem.synopsis_vector.is_(None))
        stmt = stmt.order_by(IgnoreItem.id.desc())
        if limit:
            logger.info(f"Limiting to {limit} items")
            stmt = stmt.limit(limit)
        items = list((await session.execute(stmt)).scalars())
        logger.info(f"Found {len(items)} items to process")
        updated_count = 0

        for item in items:
            logger.info(f"Processing item {item.uid}")
            attrs = item.attributes or {}
            try:
                if query_rules and await should_ignore_by_rules(item):
                    logger.info(
                        f"Skipping item {item.uid} because it's disqualified by rules"
                    )
                    continue
            except Exception as e:
                logger.error(f"Error evaluating rules for item {item.uid}: {e}")
                continue

            title = item.checked_title or item.title
            year = _year_from_attrs(attrs)
            genres = _to_list_of_str((attrs).get("genres"))
            language = _to_list_of_str((attrs).get("originalLanguage"))

            synopsis = attrs.get("synopsis") or attrs.get("ai", {}).get("synopsis")

            # --require-synopsis never generates: it only embeds the synopses
            # that already exist (the cheap path that skips gpt-5.5 entirely).
            if not require_synopsis and (force or synopsis is None):
                logger.info(f"Generating synopsis for {item.uid}")
                synopsis, _synopsis_failure = await agenerate_synopsis_for_candidate(
                    title, year, genres, language, item.item_type
                )
                if synopsis:
                    attrs["synopsis"] = synopsis

            # Drop the dead pointer from the Weaviate era.
            ai = attrs.get("ai")
            if isinstance(ai, dict) and "weaviate_uuid" in ai:
                ai.pop("weaviate_uuid", None)
                attrs["ai"] = ai

            if synopsis:
                try:
                    attrs = await upsert_item_vector(
                        attrs, item.item_type, item.uid, title, synopsis
                    )
                except Exception as e:
                    logger.error(f"Failed to write vector for {item.uid}: {e}")
                    raise

            if dry_run:
                print(
                    json.dumps(
                        {
                            "uid": item.uid,
                            "type": item.item_type,
                            "title": title,
                            "synopsis_added": bool(synopsis),
                        },
                        indent=2,
                    )
                )
            else:
                logger.info(f"Updating item {item.uid}")
                item.attributes = attrs
                flag_modified(item, "attributes")
                session.add(item)
                await session.commit()
                updated_count += 1

        print(f"Backfill complete. Updated {updated_count} item(s).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill synopsis and vectors for items"
    )
    parser.add_argument(
        "--type", choices=["mv", "tv"], default="mv", help="Filter by item type"
    )
    parser.add_argument("--limit", type=int, default=0, help="Max items to process")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview changes without saving"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Regenerate synopsis even if already present",
    )
    parser.add_argument(
        "--reindex-all",
        action="store_true",
        help="Re-embed all items even if they already have a synopsis vector",
    )
    parser.add_argument(
        "--check-vectors",
        action="store_true",
        help="Walk items that already have a vector (e.g. to sanity-check the index)",
    )
    parser.add_argument(
        "--skip-rules",
        action="store_true",
        help="Skip applying FilterRules to exclude items",
    )
    parser.add_argument(
        "--since",
        type=int,
        help="Only process items since this year",
    )
    parser.add_argument(
        "--added-only",
        action="store_true",
        help=(
            "Restrict to items the user added — the corpus the synopsis search "
            "queries. Pair with --require-synopsis to embed only those that "
            "already have a synopsis."
        ),
    )
    parser.add_argument(
        "--require-synopsis",
        action="store_true",
        help=(
            "Only process items that already have a stored synopsis; never "
            "generate one. Use to embed the existing synopsis corpus without "
            "triggering gpt-5.5 synopsis generation for the no-synopsis tail."
        ),
    )
    args = parser.parse_args()

    asyncio.run(
        backfill(
            args.type,
            args.limit,
            args.dry_run,
            args.force,
            args.reindex_all,
            args.check_vectors,
            not args.skip_rules,
            int(args.since or datetime.now().year),
            args.added_only,
            args.require_synopsis,
        )
    )


if __name__ == "__main__":
    main()
