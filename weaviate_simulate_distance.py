#!/usr/bin/env python3
import argparse
import json
from typing import Optional

from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session
from indexer_utils.weaviate_client import get_nearest_neighbors, get_weaviate_client


def pick_seed_item(item_type: str, seed_uid: Optional[str]) -> Optional[IgnoreItem]:
    session = db_session()
    q = session.query(IgnoreItem).filter(
        IgnoreItem.attributes["ai"]["weaviate_uuid"].isnot(None)
    )
    coll_name = "IgnoreItemMV" if item_type == "mv" else "IgnoreItemTV"
    client = get_weaviate_client()
    coll = client.collections.get(coll_name)
    item_ids = [r.properties.get("uid") for r in coll.query.fetch_objects().objects]
    client.close()
    if item_ids:
        q = q.filter(IgnoreItem.uid.in_(item_ids))
    if item_type:
        q = q.filter(IgnoreItem.item_type == item_type)
    if seed_uid:
        q = q.filter(IgnoreItem.uid == seed_uid)
    # most recent first
    q = q.order_by(IgnoreItem.id.desc())
    return q.first()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Simulate distance search via Weaviate and print nearest items"
    )
    parser.add_argument("--type", choices=["mv", "tv"], help="Filter by item type")
    parser.add_argument("--seed-uid", help="UID of seed item")
    parser.add_argument("--k", type=int, default=4, help="Number of neighbors to fetch")
    args = parser.parse_args()

    seed = pick_seed_item(args.type, args.seed_uid)
    if not seed:
        print("No seed item with weaviate_uuid found.")
        return

    seed_ai = (seed.attributes or {}).get("ai", {})
    weav_uuid = seed_ai.get("weaviate_uuid")
    if not weav_uuid:
        print("Seed item missing weaviate_uuid.")
        return

    item_distances = get_nearest_neighbors(weav_uuid, args.k, seed.item_type)
    neighbor_uids = [uid for uid in item_distances.keys()]
    # Look up neighbors in MySQL and print
    session = db_session()
    neighbors = (
        session.query(IgnoreItem)
        .filter(
            IgnoreItem.item_type == seed.item_type, IgnoreItem.uid.in_(neighbor_uids)
        )
        .all()
    )
    for n in neighbors:
        item_distances[n.uid]["item"] = n
    data = []
    for item_def in item_distances.values():
        data.append(
            {
                "uid": item_def["item"].uid,
                "type": item_def["item"].item_type,
                "title": item_def["item"].checked_title or item_def["item"].title,
                "ai": (item_def["item"].attributes or {}).get("ai", {}),
                "added": item_def["item"].added,
                "distance": item_def["distance"],
            }
        )
    print(
        json.dumps(
            {
                "seed": {
                    "uid": seed.uid,
                    "type": seed.item_type,
                    "title": seed.checked_title or seed.title,
                    "ai": seed_ai,
                },
                "neighbors": data,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
