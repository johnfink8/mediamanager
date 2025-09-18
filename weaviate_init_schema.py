#!/usr/bin/env python3
from typing import Any, Dict, List

import weaviate
from decouple import config
from weaviate.classes.config import (
    Configure,
    DataType,
    Property,
    Tokenization,
)

from indexer_utils.weaviate_client import get_weaviate_client


def build_class(class_name: str, embedding_model: str) -> Dict[str, Any]:
    return {
        "class": class_name,
        "description": "IgnoreItem vectors for similarity search",
        "vectorizer": "text2vec-openai",
        "moduleConfig": {
            "text2vec-openai": {
                "model": embedding_model,
                # dimensions are inferred by Weaviate for OpenAI models since v1.25,
                # but it's okay to be explicit for compatibility
                "dimensions": 1536,
                "type": "text",
            }
        },
        "vectorIndexType": "hnsw",
        "vectorIndexConfig": {
            "efConstruction": 128,
            "maxConnections": 64,
        },
        "properties": [
            {
                "name": "uid",
                "dataType": DataType.BLOB,
                "description": "Unique ID from the app (IMDB/TVDB)",
            },
            {
                "name": "title",
                "dataType": DataType.TEXT,
                "tokenization": Tokenization.WORD,
            },
            {
                "name": "type",
                "dataType": DataType.BLOB,
                "tokenization": Tokenization.FIELD,
            },
            {
                "name": "synopsis",
                "dataType": DataType.TEXT,
                "tokenization": Tokenization.WORD,
            },
        ],
    }


def ensure_classes(client: weaviate.WeaviateClient, embedding_model: str) -> None:
    target_classes: List[str] = ["IgnoreItemMV", "IgnoreItemTV"]
    client.collections.delete_all()
    try:
        for name in target_classes:
            print(f"Creating class {name}...")
            client.collections.create(
                name,
                description="IgnoreItem vectors for similarity search",
                properties=[
                    Property(
                        name="uid", data_type=DataType.TEXT, skip_vectorization=True
                    ),
                    Property(name="title", data_type=DataType.TEXT),
                    Property(
                        name="type", data_type=DataType.TEXT, skip_vectorization=True
                    ),
                    Property(name="synopsis", data_type=DataType.TEXT),
                ],
                vector_config=Configure.Vectors.text2vec_openai(
                    name="default",
                    model=embedding_model,
                    source_properties=["title", "synopsis"],
                ),
            )
            print(f"Created class {name}")
    finally:
        client.close()


def main() -> None:
    embedding_model = config("OPENAI_EMBEDDING_MODEL", default="text-embedding-3-small")

    if not config("OPENAI_API_KEY", default=None):
        print("WARNING: OPENAI_API_KEY not set. text2vec-openai will not work.")

    client = get_weaviate_client()

    ensure_classes(client, embedding_model)
    print("Weaviate schema ensured.")


if __name__ == "__main__":
    main()
