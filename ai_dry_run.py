#!/usr/bin/env python3
import argparse
import json
from typing import List, Optional

from decouple import config

from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session
from indexer_utils.vid_utils import annotate_attributes_for_item


def load_items(
    item_type: Optional[str], uid: Optional[str], limit: int
) -> List[IgnoreItem]:
    with db_session() as session:
        query = session.query(IgnoreItem).where(IgnoreItem.ignore.is_(False))
        if item_type:
            query = query.where(IgnoreItem.item_type == item_type)
        if uid:
            query = query.where(IgnoreItem.uid == uid)
            return query.all()
        # Prefer most recent by created_at desc then id desc
        try:
            query = query.order_by(IgnoreItem.created_at.desc(), IgnoreItem.id.desc())
        except Exception:
            query = query.order_by(IgnoreItem.id.desc())
        return query.limit(limit).all()


def main() -> None:
    parser = argparse.ArgumentParser(description="Dry-run AI recommendation preview")
    parser.add_argument(
        "--type", choices=["mv", "tv"], default="mv", help="Filter by item type"
    )
    parser.add_argument("--uid", help="Run for a specific UID")
    parser.add_argument(
        "--limit", type=int, default=5, help="Max items if no UID provided"
    )
    parser.add_argument(
        "--show-prompts",
        action="store_true",
        help="Print system prompt and user payload context",
    )
    args = parser.parse_args()

    if not config("OPENAI_API_KEY"):
        print("WARNING: OPENAI_API_KEY is not set; API result will be null.")

    items = load_items(args.type, args.uid, args.limit)
    if not items:
        print("No matching items found.")
        return

    for item in items:
        attrs = item.attributes or {}
        title = item.checked_title or item.title
        annotated = annotate_attributes_for_item(item.item_type, item.uid, title, attrs)

        item_info = {
            "uid": item.uid,
            "type": item.item_type,
            "title": item.title,
            "checked_title": item.checked_title,
            "poster_url": item.poster_url,
            "created_at": item.created_at,
            "attributes": attrs,
        }

        output = {
            "item": item_info,
            "annotated_attributes": annotated,
        }

        print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
