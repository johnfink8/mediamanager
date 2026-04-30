import asyncio
from typing import Any, Dict, List
from uuid import uuid4

import weaviate
from decouple import config
from weaviate.classes.query import MetadataQuery


def _request_headers() -> dict:
    return {"X-OpenAI-Api-Key": config("OPENAI_API_KEY")}


def _class_name(item_type: str) -> str:
    return "IgnoreItemMV" if item_type == "mv" else "IgnoreItemTV"


def get_weaviate_client():
    client = weaviate.connect_to_local(
        host=config("WEAVIATE_HOST", default="weaviate"),
        headers=_request_headers(),
    )
    return client


def get_nearest_neighbors(uuid: str, k: int, item_type: str):
    client = get_weaviate_client()
    coll = client.collections.get(_class_name(item_type))

    items = coll.query.near_object(
        uuid,
        limit=k,
        return_metadata=MetadataQuery(distance=True),
    )
    item_distances = {
        item.properties.get("uid"): {
            "uid": item.properties.get("uid"),
            "distance": item.metadata.distance,
        }
        for item in items.objects
        if item.properties.get("uuid") != uuid
    }
    client.close()
    return item_distances


def search_by_synopsis(query_text: str, k: int, item_type: str) -> List[Dict[str, Any]]:
    client = get_weaviate_client()
    coll = client.collections.get(_class_name(item_type))
    items = coll.query.near_text(
        query=query_text,
        limit=k,
        return_metadata=MetadataQuery(distance=True),
    )
    results = [
        {
            "uid": obj.properties.get("uid"),
            "title": obj.properties.get("title"),
            "synopsis": obj.properties.get("synopsis"),
            "distance": obj.metadata.distance,
        }
        for obj in items.objects
    ]
    client.close()
    return results


async def asearch_by_synopsis(
    query_text: str, k: int, item_type: str
) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(search_by_synopsis, query_text, k, item_type)


async def aget_nearest_neighbors(uuid: str, k: int, item_type: str):
    return await asyncio.to_thread(get_nearest_neighbors, uuid, k, item_type)


def upsert_item_attrs(attrs: dict, item_type: str, uid, title, synopsis):
    client = get_weaviate_client()
    ai = attrs.get("ai", {})
    weav_uuid = ai.get("weaviate_uuid")
    if not weav_uuid:
        weav_uuid = str(uuid4())
        ai["weaviate_uuid"] = weav_uuid
        attrs["ai"] = ai
    data_obj = {
        "uid": uid,
        "title": title,
        "type": item_type,
        "synopsis": synopsis,
    }
    coll = client.collections.get(_class_name(item_type))
    if coll.data.exists(uuid=weav_uuid):
        coll.data.update(properties=data_obj, uuid=weav_uuid)
    else:
        coll.data.insert(properties=data_obj, uuid=weav_uuid)
    client.close()
    return attrs


async def aupsert_item_attrs(attrs: dict, item_type: str, uid, title, synopsis):
    return await asyncio.to_thread(
        upsert_item_attrs, attrs, item_type, uid, title, synopsis
    )
