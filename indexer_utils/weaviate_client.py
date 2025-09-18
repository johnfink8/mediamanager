from uuid import uuid4

import weaviate
from decouple import config
from weaviate.classes.query import MetadataQuery

headers = {
    "X-OpenAI-Api-Key": config("OPENAI_API_KEY"),
}


def get_weaviate_client():
    client = weaviate.connect_to_local(
        host=config("WEAVIATE_HOST", default="weaviate"),
        headers=headers,
    )
    return client


def get_nearest_neighbors(uuid: str, k: int, item_type: str):
    client = get_weaviate_client()
    class_name = "IgnoreItemMV" if item_type == "mv" else "IgnoreItemTV"
    coll = client.collections.get(class_name)

    # Fetch the vector of the seed item from Weaviate
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


def upsert_item_attrs(attrs: dict, item_type: str, uid, title, synopsis):
    client = get_weaviate_client()
    class_name = "IgnoreItemMV" if item_type == "mv" else "IgnoreItemTV"
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
    coll = client.collections.get(class_name)
    if coll.data.exists(uuid=weav_uuid):
        coll.data.update(properties=data_obj, uuid=weav_uuid)
    else:
        coll.data.insert(properties=data_obj, uuid=weav_uuid)
    client.close()
    return attrs
