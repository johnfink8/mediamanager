#!/usr/bin/env python3
import argparse
import json
from typing import List

import weaviate

from indexer_utils.weaviate_client import get_weaviate_client


def fetch_vectors(
    client: "weaviate.WeaviateClient", class_name: str, limit: int
) -> List[dict]:
    coll = client.collections.get(class_name)
    result = (
        coll.query.near_text("This is a sample query")
        .with_additional(["id", "vector"])
        .with_limit(limit)
        .do()
    )
    return result.get("data", {}).get("Get", {}).get(class_name, [])


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect vectors stored in Weaviate")
    parser.add_argument(
        "--class", dest="cls", choices=["mv", "tv", "all"], default="all"
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument(
        "--dims",
        type=int,
        default=5,
        help="Print only first N dimensions of the vector",
    )
    args = parser.parse_args()

    client = get_weaviate_client()

    classes = []
    if args.cls == "all":
        classes = ["IgnoreItemMV", "IgnoreItemTV"]
    else:
        classes = ["IgnoreItemMV" if args.cls == "mv" else "IgnoreItemTV"]

    for class_name in classes:
        print(f"Class: {class_name}")
        items = fetch_vectors(client, class_name, args.limit)
        print(f"Count: {len(items)}")
        for i, obj in enumerate(items, start=1):
            uid = obj.get("uid")
            title = obj.get("title")
            add = obj.get("_additional", {})
            vec = add.get("vector")
            synopsis = obj.get("synopsis")
            if vec is None:
                print(
                    json.dumps(
                        {"idx": i, "uid": uid, "title": title, "vector": None}, indent=2
                    )
                )
                continue
            to_print = vec if args.dims is None else vec[: args.dims]
            print(
                json.dumps(
                    {
                        "idx": i,
                        "uid": uid,
                        "title": title,
                        "vector_dims": len(vec),
                        "vector": to_print,
                        "synopsis": synopsis,
                    },
                    indent=2,
                )
            )
    client.close()


if __name__ == "__main__":
    main()
