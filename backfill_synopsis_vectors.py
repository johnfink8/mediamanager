#!/usr/bin/env python3
"""Backfill ``synopsis_vector`` on ``indexer_utils_ignoreitem``.

Walks items that don't yet have a vector (or all items with ``--reindex-all``),
makes sure each has an AI-generated synopsis, embeds it, and writes the
result into pgvector via ``vector_search.upsert_item_vector``.
"""

import argparse
import asyncio
import json
import logging
from datetime import datetime
from typing import List, Optional

from decouple import config
from sqlalchemy import Integer, and_, func, or_, select, text
from sqlalchemy.orm.attributes import flag_modified

from indexer_utils.ai_recs import agenerate_synopsis_for_candidate
from indexer_utils.filters import should_ignore_by_rules
from indexer_utils.models import FilterRule, IgnoreItem
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
) -> None:
    if not config("OPENAI_API_KEY", default=None):
        print(
            "WARNING: OPENAI_API_KEY is not set; backfill will not generate any data."
        )
        return

    async with db_session() as session:
        stmt = select(IgnoreItem).where(IgnoreItem.item_type == item_type)
        if added_only:
            stmt = stmt.where(
                IgnoreItem.added.is_(True),
                IgnoreItem.attributes["ai"]["synopsis"].isnot(None),
            )
            logger.info("Restricting to added=True items with stored synopses")
        logger.info(f"Querying {item_type} items")

        if query_rules:
            rules = list(
                (
                    await session.execute(
                        select(FilterRule).filter_by(enabled=True, item_type=item_type)
                    )
                ).scalars()
            )

            disq_clauses = []
            logger.info(f"Querying {len(rules)} FilterRules")
            for r in rules:
                if r.attribute in [
                    "type",
                    "uid",
                    "title",
                    "checked_title",
                    "poster_url",
                    "added",
                    "ignore",
                ]:
                    field = getattr(IgnoreItem, r.attribute)
                else:
                    field = IgnoreItem.attributes[r.attribute]
                op = r.operator
                val = r.value
                cond = None
                if op == "eq":
                    cond = field.contains(val)
                elif op == "neq":
                    cond = ~field.contains(val)
                elif op == "in":
                    values = [v.strip() for v in val.split(",")]
                    cond = or_(*[field.contains(v) for v in values])
                elif op == "not_in":
                    values = [v.strip() for v in val.split(",")]
                    cond = and_(*[~field.contains(v) for v in values])
                elif op == "lt":
                    cond = func.cast(field[0], Integer) < int(val)
                elif op == "gt":
                    cond = func.cast(field[0], Integer) > int(val)
                elif op == "lte":
                    cond = func.cast(field[0], Integer) <= int(val)
                elif op == "gte":
                    cond = func.cast(field[0], Integer) >= int(val)
                elif op == "contains":
                    cond = field.contains(val)
                elif op == "not_contains":
                    cond = ~field.contains(val)
                else:
                    continue
                disq_clauses.append(and_(IgnoreItem.item_type == r.item_type, cond))

            if disq_clauses:
                stmt = stmt.where(~or_(*disq_clauses))

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

            synopsis = attrs.get("ai", {}).get("synopsis")

            if force or synopsis is None:
                logger.info(f"Generating synopsis for {item.uid}")
                synopsis, _synopsis_failure = await agenerate_synopsis_for_candidate(
                    title, year, genres, language, item.item_type
                )
                if synopsis:
                    ai = attrs.get("ai", {})
                    ai["synopsis"] = synopsis
                    attrs["ai"] = ai

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
            "Only embed items the user added with an existing stored synopsis. "
            "Targets exactly what the search tool returns and keeps cost bounded."
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
        )
    )


if __name__ == "__main__":
    main()
