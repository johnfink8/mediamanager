#!/usr/bin/env python3
import argparse
import json
import logging
from typing import List, Optional

from decouple import config
from sqlalchemy import Integer, and_, func, or_
from sqlalchemy.orm.attributes import flag_modified

from indexer_utils.filters import should_ignore_by_rules
from indexer_utils.models import FilterRule, IgnoreItem
from indexer_utils.session import db_session
from indexer_utils.vid_utils import (
    generate_synopsis_for_candidate,  # reuse existing helpers
)
from indexer_utils.weaviate_client import get_weaviate_client, upsert_item_attrs

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


def backfill(item_type: str, limit: int, dry_run: bool, force: bool) -> None:
    if not config("OPENAI_API_KEY", default=None):
        print(
            "WARNING: OPENAI_API_KEY is not set; backfill will not generate any data."
        )
        return

    coll_name = "IgnoreItemMV" if item_type == "mv" else "IgnoreItemTV"
    client = get_weaviate_client()
    coll = client.collections.get(coll_name)
    item_ids = [r.properties.get("uid") for r in coll.query.fetch_objects().objects]
    client.close()

    session = db_session()
    query = session.query(IgnoreItem)
    query = query.filter(IgnoreItem.item_type == item_type)
    if item_ids:
        query = query.filter(~IgnoreItem.uid.in_(item_ids))

    # Exclude items that would be disqualified by rules by pushing rules into SQL
    rules_q = session.query(FilterRule).filter_by(enabled=True)
    rules_q = rules_q.filter_by(item_type=item_type)
    rules = rules_q.all()

    disq_clauses = []
    for r in rules:
        # Map rule.attribute to column or JSON field
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
        query = query.filter(~or_(*disq_clauses))

    # Only backfill items not yet upserted to Weaviate
    query = query.filter(IgnoreItem.attributes["ai"]["weaviate_uuid"].is_(None))
    query = query.order_by(IgnoreItem.id.desc()).limit(limit)
    items = query.all()
    logger.info(f"Found {len(items)} items to process")
    updated_count = 0

    for item in items:
        logger.info(f"Processing item {item.uid}")
        attrs = item.attributes or {}
        # Apply FilterRule logic: skip items disqualified by rules
        try:
            if should_ignore_by_rules(item):
                logger.info(
                    f"Skipping item {item.uid} because it's disqualified by rules"
                )
                continue
        except Exception as e:
            # Be conservative; if rule evaluation fails, skip the item
            logger.error(f"Error evaluating rules for item {item.uid}: {e}")
            continue

        title = item.checked_title or item.title
        year = _year_from_attrs(attrs)
        genres = _to_list_of_str((attrs).get("genres"))
        language = _to_list_of_str((attrs).get("originalLanguage"))

        synopsis = attrs.get("ai", {}).get("synopsis")
        if force or synopsis is None:
            logger.info(f"Generating synopsis for {item.uid}")
            synopsis, _synopsis_failure = generate_synopsis_for_candidate(
                title, year, genres, language, item.item_type
            )
            if synopsis:
                ai = attrs.get("ai", {})
                ai["synopsis"] = synopsis
                attrs["ai"] = ai

        # Index into Weaviate (store vector external to MySQL)
        if synopsis:
            try:
                attrs = upsert_item_attrs(
                    attrs, item.item_type, item.uid, title, synopsis
                )
            except Exception as e:
                logger.error(f"Failed to upsert into Weaviate for {item.uid}: {e}")
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
            logger.info(f"Updating item {item.uid}:{attrs}")
            item.attributes = attrs
            flag_modified(item, "attributes")
            session.add(item)
            session.commit()
            updated_count += 1

    print(f"Backfill complete. Updated {updated_count} item(s).")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill synopsis and vectors for items"
    )
    parser.add_argument(
        "--type", choices=["mv", "tv"], default="mv", help="Filter by item type"
    )
    parser.add_argument("--limit", type=int, default=1, help="Max items to process")
    parser.add_argument(
        "--dry-run", action="store_true", help="Preview changes without saving"
    )
    parser.add_argument(
        "--force", action="store_true", help="Regenerate even if already present"
    )
    args = parser.parse_args()

    backfill(args.type, args.limit, args.dry_run, args.force)


if __name__ == "__main__":
    main()
