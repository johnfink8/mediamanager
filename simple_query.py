from indexer_utils.weaviate_client import get_weaviate_client


def main():
    client = get_weaviate_client()
    coll = client.collections.get("IgnoreItemTV")
    result = coll.query.fetch_objects()
    print([r.properties.get("uid") for r in result.objects])
    client.close()


if __name__ == "__main__":
    main()
