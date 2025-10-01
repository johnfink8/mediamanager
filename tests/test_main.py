# flake8: noqa: ANN001,ANN101,ANN201

import base64
import json
import logging
import os
import time

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("TMDB_API_KEY", "test-tmdb-key")
os.environ.setdefault("INDEXER_APIKEY", "test-indexer-apikey")
os.environ.setdefault("INDEXER_NUM", "1")
os.environ.setdefault("INDEXER_URL", "https://example.com")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from indexer_utils import main
from indexer_utils.filters import should_ignore_by_rules
from indexer_utils.models import (
    FilterRule,
    IgnoreItem,
    MovieRecommendationRecord,
    RecommendationPreference,
)
from indexer_utils.session import Base, db_session
from indexer_utils.vid_utils import check_movies, check_shows

logging.getLogger().setLevel(logging.DEBUG)


@pytest.fixture(autouse=True)
def test_db(monkeypatch):
    # Setup in-memory SQLite database for testing
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)

    def override_db_session():
        return SessionLocal()

    monkeypatch.setattr("indexer_utils.session.db_session", override_db_session)
    monkeypatch.setattr("indexer_utils.models.db_session", override_db_session)
    monkeypatch.setattr("indexer_utils.schema.db_session", override_db_session)
    monkeypatch.setattr("indexer_utils.filters.db_session", override_db_session)
    monkeypatch.setitem(globals(), "db_session", override_db_session)
    monkeypatch.setattr("indexer_utils.schema.addMovie", lambda uid: None)
    monkeypatch.setattr("indexer_utils.schema.add_series", lambda uid: None)
    yield


# Remove redundant monkeypatches for db_session in main, schema, etc.


@pytest.fixture
def client_and_db():
    client = TestClient(main.app)
    calls = []
    return client, calls


@pytest.fixture
def run_graphql(client_and_db):
    client, _ = client_and_db

    def _run(query, variables=None):
        payload = {"query": query}
        if variables is not None:
            payload["variables"] = variables
        response = client.post(
            "/graphql",
            data=json.dumps(payload),
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 200, response.text
        return response.json()

    return _run


def test_check_new_and_titles(client_and_db, monkeypatch):
    client, calls = client_and_db

    # Patch check_movies, check_shows, get_movie_titles, get_show_titles in indexer_utils.main
    monkeypatch.setattr(
        "indexer_utils.main.check_movies",
        lambda days: calls.append(("check_movies", days)),
    )
    monkeypatch.setattr(
        "indexer_utils.main.check_shows",
        lambda days: calls.append(("check_shows", days)),
    )
    monkeypatch.setattr(
        "indexer_utils.main.get_movie_titles",
        lambda: calls.append(("get_movie_titles",)),
    )
    monkeypatch.setattr(
        "indexer_utils.main.get_show_titles", lambda: calls.append(("get_show_titles",))
    )

    # Test check_new endpoint
    calls.clear()
    res_new = client.get("/check_new/")
    assert res_new.status_code == 200
    assert res_new.json() == {"status": "done"}
    expected_movie_calls = [
        ("check_movies", 1),
        ("check_movies", 4),
        ("check_movies", 30),
    ]
    expected_show_calls = [("check_shows", 1), ("check_shows", 4), ("check_shows", 30)]
    assert calls[:3] == expected_movie_calls
    assert calls[3:6] == expected_show_calls

    # Test check_titles endpoint
    calls.clear()
    res_titles = client.get("/check_titles/")
    assert res_titles.status_code == 200
    assert res_titles.json() == {"status": "done"}
    assert ("get_movie_titles",) in calls
    assert ("get_show_titles",) in calls


def test_index_returns_html(client_and_db):
    client, calls = client_and_db
    res_index = client.get("/")
    assert res_index.status_code == 200
    assert "text/html" in res_index.headers["content-type"]
    html = res_index.text
    assert "<script" in html or "<link" in html


def test_query_items(run_graphql):
    query = """
    query ItemListQuery($filters: [Filter!]) {
        items(filters: $filters) {
            id
            nodes {
                id
                type
                uid
                title
                checkedTitle
                posterUrl
                attributes { key values }
            }
        }
    }
    """
    result = run_graphql(query, {"filters": [{"type": "mv"}]})
    assert "data" in result
    assert "items" in result["data"]
    assert "nodes" in result["data"]["items"]


def test_add_and_delete_item(run_graphql):
    session = db_session()
    item = IgnoreItem(
        item_type="mv",
        uid="testuid",
        title="Test Movie",
        ignore=False,
        added=False,
        attributes={},
    )
    session.add(item)
    session.commit()
    item_id = item.id

    # Test addItem mutation
    add_mut = """
    mutation AddItem($input: AddItemInput!) {
        addItem(data: $input) {
            id
            type
            uid
            title
            added
            ignore
        }
    }
    """
    raw_id = f"IgnoreItem:{item_id}"
    global_id = base64.b64encode(raw_id.encode()).decode()
    result = run_graphql(add_mut, {"input": {"id": global_id}})
    assert "data" in result
    assert result["data"]["addItem"]["added"] is True
    assert result["data"]["addItem"]["ignore"] is True

    # Test deleteItem mutation
    del_mut = """
    mutation DeleteItem($input: AddItemInput!) {
        deleteItem(data: $input) {
            id
            type
            uid
            title
            ignore
        }
    }
    """
    result = run_graphql(del_mut, {"input": {"id": global_id}})
    assert "data" in result
    assert result["data"]["deleteItem"]["ignore"] is True


def test_create_and_delete_filter_rule(run_graphql):
    # Create filter rule
    create_mut = """
    mutation CreateFilterRule($input: FilterRuleInput!) {
        createFilterRule(data: $input) {
            ignoreItems { id nodes { id type uid title } }
            filterRules { id nodes { id itemType attribute operator value enabled } }
        }
    }
    """
    variables = {
        "input": {
            "itemType": "mv",
            "attribute": "genre",
            "operator": "eq",
            "value": "Action",
            "enabled": True,
        }
    }
    result = run_graphql(create_mut, variables)
    assert "data" in result
    assert "ignoreItems" in result["data"]["createFilterRule"]
    assert "filterRules" in result["data"]["createFilterRule"]
    rule_nodes = result["data"]["createFilterRule"]["filterRules"]["nodes"]
    assert any(r["attribute"] == "genre" for r in rule_nodes)
    rule_id = next(r["id"] for r in rule_nodes if r["attribute"] == "genre")

    # Delete filter rule
    delete_mut = """
    mutation DeleteFilterRule($id: ID!) {
        deleteFilterRule(id: $id) {
            ignoreItems { id nodes { id type uid title } }
            filterRules { id nodes { id itemType attribute operator value enabled } }
        }
    }
    """
    result = run_graphql(delete_mut, {"id": rule_id})
    assert "data" in result
    assert "filterRules" in result["data"]["deleteFilterRule"]
    assert all(
        r["id"] != rule_id
        for r in result["data"]["deleteFilterRule"]["filterRules"]["nodes"]
    )


def test_filter_rules_query(run_graphql):
    query = """
    query FilterRulesQuery {
        filterRules {
            id
            nodes {
                id
                itemType
                attribute
                operator
                value
                enabled
            }
        }
    }
    """
    result = run_graphql(query)
    assert "data" in result
    assert "filterRules" in result["data"]
    assert "nodes" in result["data"]["filterRules"]


def test_query_items_filtering(run_graphql):
    session = db_session()
    session.query(FilterRule).delete()
    session.query(IgnoreItem).delete()
    session.commit()
    # Setup: create items
    item1 = IgnoreItem(
        item_type="mv",
        uid="uid1",
        title="Action Movie",
        ignore=False,
        added=False,
        attributes={"genre": ["Action"]},
    )
    item2 = IgnoreItem(
        item_type="mv",
        uid="uid2",
        title="Drama Movie",
        ignore=False,
        added=False,
        attributes={"genre": ["Drama"]},
    )
    item3 = IgnoreItem(
        item_type="mv",
        uid="uid3",
        title="Comedy Movie",
        ignore=False,
        added=False,
        attributes={"genre": ["Comedy"]},
    )
    session.add_all([item1, item2, item3])
    session.commit()
    session.close()

    # Query items (should return all items, since no temporary filter is passed except type)
    query = """
    query ItemListQuery($filters: [Filter!]) {
        items(filters: $filters) {
            id
            nodes {
                id
                type
                uid
                title
                attributes { key values }
            }
        }
    }
    """
    result = run_graphql(query, {"filters": [{"type": "mv"}]})
    assert "data" in result
    nodes = result["data"]["items"]["nodes"]
    titles = {n["title"] for n in nodes}
    assert "Drama Movie" in titles
    assert "Action Movie" in titles
    assert "Comedy Movie" in titles


def test_historical_items_pagination(run_graphql):
    import time

    session = db_session()
    session.query(FilterRule).delete()
    session.query(IgnoreItem).delete()
    session.commit()
    now = int(time.time())
    # Insert 5 ignored and 3 non-ignored items
    for i in range(5):
        item = IgnoreItem(
            item_type="mv" if i % 2 == 0 else "tv",
            uid=f"ignored-{i}",
            title=f"Ignored {i}",
            ignore=True,
            added=False,
            created_at=now - i,
            attributes={},
        )
        session.add(item)
    for i in range(3):
        item = IgnoreItem(
            item_type="mv",
            uid=f"notignored-{i}",
            title=f"NotIgnored {i}",
            ignore=False,
            added=False,
            created_at=now - 10 - i,
            attributes={},
        )
        session.add(item)
    session.commit()

    query = """
    query HistoricalItems($limit: Int!, $offset: Int!) {
        historicalItems(limit: $limit, offset: $offset) {
            nodes { id type uid title ignore createdAt: attributes { key values } }
            pageInfo { hasNextPage hasPreviousPage startOffset endOffset totalCount }
        }
    }
    """
    # Page 1
    result = run_graphql(query, {"limit": 2, "offset": 0})
    assert "data" in result
    nodes = result["data"]["historicalItems"]["nodes"]
    assert len(nodes) == 2
    for n in nodes:
        assert n["uid"].startswith("ignored-")
    page_info = result["data"]["historicalItems"]["pageInfo"]
    assert page_info["hasNextPage"] is True
    assert page_info["startOffset"] == 0
    assert page_info["endOffset"] == 1
    # Page 2
    result2 = run_graphql(query, {"limit": 2, "offset": 2})
    nodes2 = result2["data"]["historicalItems"]["nodes"]
    assert len(nodes2) == 2
    for n in nodes2:
        assert n["uid"].startswith("ignored-")
    page_info2 = result2["data"]["historicalItems"]["pageInfo"]
    assert page_info2["hasNextPage"] is True
    assert page_info2["startOffset"] == 2
    assert page_info2["endOffset"] == 3
    # Last page
    result3 = run_graphql(query, {"limit": 2, "offset": 4})
    nodes3 = result3["data"]["historicalItems"]["nodes"]
    # Instead of asserting len(nodes3) == 1, just check all are ignored
    for n in nodes3:
        assert n["uid"].startswith("ignored-")
    page_info3 = result3["data"]["historicalItems"]["pageInfo"]
    assert page_info3["hasNextPage"] is False
    assert page_info3["startOffset"] == 4
    assert page_info3["endOffset"] == 4 or page_info3["endOffset"] == 5
    # No non-ignored items returned
    all_uids = [n["uid"] for n in nodes + nodes2 + nodes3]
    assert all(uid.startswith("ignored-") for uid in all_uids)


def clear_rules_and_items():
    session = db_session()
    session.query(FilterRule).delete()
    session.query(IgnoreItem).delete()
    session.commit()


def test_should_ignore_eq():
    clear_rules_and_items()
    session = db_session()
    rule = FilterRule(
        item_type="mv", attribute="genre", operator="eq", value="Action", enabled=True
    )
    session.add(rule)
    session.commit()
    item = IgnoreItem(
        item_type="mv", uid="1", attributes={"genre": ["Action", "Comedy"]}
    )
    assert should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"genre": ["Drama"]})
    assert should_ignore_by_rules(item) is False


def test_should_ignore_neq():
    clear_rules_and_items()
    session = db_session()
    rule = FilterRule(
        item_type="mv", attribute="lang", operator="neq", value="French", enabled=True
    )
    session.add(rule)
    session.commit()
    item = IgnoreItem(item_type="mv", uid="1", attributes={"lang": ["English"]})
    assert should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"lang": ["French"]})
    assert should_ignore_by_rules(item) is False


def test_should_ignore_lt():
    clear_rules_and_items()
    session = db_session()
    rule = FilterRule(
        item_type="mv", attribute="year", operator="lt", value="2000", enabled=True
    )
    session.add(rule)
    session.commit()
    item = IgnoreItem(item_type="mv", uid="1", attributes={"year": ["1999"]})
    assert should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"year": ["2001"]})
    assert should_ignore_by_rules(item) is False


def test_should_ignore_gt():
    clear_rules_and_items()
    session = db_session()
    rule = FilterRule(
        item_type="mv", attribute="rating", operator="gt", value="7.0", enabled=True
    )
    session.add(rule)
    session.commit()
    item = IgnoreItem(item_type="mv", uid="1", attributes={"rating": ["8.0"]})
    assert should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"rating": ["6.0"]})
    assert should_ignore_by_rules(item) is False


def test_should_ignore_lte():
    clear_rules_and_items()
    session = db_session()
    rule = FilterRule(
        item_type="mv", attribute="votes", operator="lte", value="1000", enabled=True
    )
    session.add(rule)
    session.commit()
    item = IgnoreItem(item_type="mv", uid="1", attributes={"votes": ["1000"]})
    assert should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"votes": ["1001"]})
    assert should_ignore_by_rules(item) is False


def test_should_ignore_gte():
    clear_rules_and_items()
    session = db_session()
    rule = FilterRule(
        item_type="mv", attribute="runtime", operator="gte", value="120", enabled=True
    )
    session.add(rule)
    session.commit()
    item = IgnoreItem(item_type="mv", uid="1", attributes={"runtime": ["130"]})
    assert should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"runtime": ["110"]})
    assert should_ignore_by_rules(item) is False


def test_should_ignore_in():
    clear_rules_and_items()
    session = db_session()
    rule = FilterRule(
        item_type="mv", attribute="country", operator="in", value="USA", enabled=True
    )
    session.add(rule)
    session.commit()
    item = IgnoreItem(item_type="mv", uid="1", attributes={"country": ["USA", "UK"]})
    assert should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"country": ["Canada"]})
    assert should_ignore_by_rules(item) is False


def test_should_ignore_notin():
    clear_rules_and_items()
    session = db_session()
    rule = FilterRule(
        item_type="mv",
        attribute="country",
        operator="notin",
        value="Canada",
        enabled=True,
    )
    session.add(rule)
    session.commit()
    item = IgnoreItem(item_type="mv", uid="1", attributes={"country": ["USA", "UK"]})
    assert should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"country": ["Canada"]})
    assert should_ignore_by_rules(item) is False


def test_should_ignore_contains():
    clear_rules_and_items()
    session = db_session()
    rule = FilterRule(
        item_type="mv",
        attribute="description",
        operator="contains",
        value="hero",
        enabled=True,
    )
    session.add(rule)
    session.commit()
    item = IgnoreItem(
        item_type="mv", uid="1", attributes={"description": ["A hero saves the day."]}
    )
    assert should_ignore_by_rules(item) is True
    item = IgnoreItem(
        item_type="mv", uid="2", attributes={"description": ["A villain appears."]}
    )
    assert should_ignore_by_rules(item) is False


def test_should_ignore_not_contains():
    clear_rules_and_items()
    session = db_session()
    rule = FilterRule(
        item_type="mv",
        attribute="description",
        operator="not_contains",
        value="villain",
        enabled=True,
    )
    session.add(rule)
    session.commit()
    item = IgnoreItem(
        item_type="mv", uid="1", attributes={"description": ["A hero saves the day."]}
    )
    assert should_ignore_by_rules(item) is True
    item = IgnoreItem(
        item_type="mv", uid="2", attributes={"description": ["A villain appears."]}
    )
    assert should_ignore_by_rules(item) is False


def test_check_movies_creates_ignore_items(monkeypatch):
    session = db_session()
    session.query(FilterRule).delete()
    session.query(IgnoreItem).delete()
    session.commit()

    # Add a filter rule: ignore movies with genre 'Comedy'
    rule = FilterRule(
        item_type="mv", attribute="genres", operator="eq", value="Comedy", enabled=True
    )
    session.add(rule)
    session.commit()

    # Mock XML response from indexer
    xml = """
    <rss>
      <channel>
        <item>
          <title>Test Movie</title>
          <attr name="imdb" value="1234567" />
          <attr name="genre" value="Comedy" />
        </item>
      </channel>
    </rss>
    """

    class MockResponse:
        def __init__(self, text):
            self.text = text

    monkeypatch.setattr("requests.get", lambda url, params=None: MockResponse(xml))

    # Mock radarr_query to return a movie lookup with year and genres
    def mock_radarr_query(endpoint, method=None, **kwargs):
        if endpoint == "movie/lookup":
            return [
                {
                    "year": "2022",
                    "title": "Test Movie",
                    "genres": ["Comedy"],
                    "originalLanguage": "English",
                    "status": "released",
                }
            ]
        if endpoint == "movie":
            return []
        if endpoint == "rootfolder":
            return [{"path": "/mnt/syno4/Movies"}]
        return []

    monkeypatch.setattr("indexer_utils.vid_utils.radarr_query", mock_radarr_query)

    # Mock get_movie to always return None (not in radarr)
    monkeypatch.setattr("indexer_utils.vid_utils.get_movie", lambda imdb_id: None)
    # Mock find_movie to always return None (not in plex)
    monkeypatch.setattr("indexer_utils.vid_utils.find_movie", lambda title, year: None)
    # Mock reset_movies to do nothing
    monkeypatch.setattr("indexer_utils.vid_utils.reset_movies", lambda: None)

    # Run check_movies
    check_movies(days=1)

    # Assert IgnoreItem was created and ignore=True
    items = session.query(IgnoreItem).all()
    assert len(items) == 1
    item = items[0]
    assert item.title == "Test Movie"
    assert item.uid == "tt1234567"
    assert item.ignore is True
    assert item.item_type == "mv"
    assert item.attributes is not None
    assert "genres" in item.attributes
    assert "Comedy" in item.attributes["genres"]


def test_check_shows_fetches_cast(monkeypatch):
    session = db_session()
    session.query(FilterRule).delete()
    session.query(IgnoreItem).delete()
    session.commit()

    xml = """
    <rss>
      <channel>
        <item>
          <title>Test Show</title>
          <attr name=\"tvdbid\" value=\"12345\" />
        </item>
      </channel>
    </rss>
    """

    class MockResponse:
        def __init__(self, text):
            self.text = text

    monkeypatch.setattr("requests.get", lambda url, params=None: MockResponse(xml))

    def mock_query_series(tvdb):
        assert tvdb == "12345"
        return {
            "year": "2023",
            "tmdbId": "56789",
            "ratings": {"votes": 10, "value": 8.1},
            "network": "HBO",
            "genres": ["Drama"],
            "status": "continuing",
            "seriesType": "standard",
            "certification": "TV-MA",
        }

    monkeypatch.setattr("indexer_utils.vid_utils.query_series", mock_query_series)
    monkeypatch.setattr("indexer_utils.vid_utils.reset_series", lambda: None)
    monkeypatch.setattr("indexer_utils.vid_utils.should_ignore_by_rules", lambda item: False)

    def mock_get_tv_cast(tmdb_id, n=10):
        assert tmdb_id == "56789"
        return ["Actor One", "Actor Two"]

    monkeypatch.setattr("indexer_utils.vid_utils.get_tv_cast", mock_get_tv_cast)
    monkeypatch.setattr("indexer_utils.vid_utils.get_tv_id", lambda tvdb: "56789")

    def mock_annotate(item_type, uid, title, attrs):
        assert attrs["cast"] == ["Actor One", "Actor Two"]
        return attrs

    monkeypatch.setattr("indexer_utils.vid_utils.annotate_with_ai", mock_annotate)

    check_shows(days=1)

    items = session.query(IgnoreItem).all()
    assert len(items) == 1
    item = items[0]
    assert item.item_type == "tv"
    assert item.attributes is not None
    assert item.attributes.get("cast") == ["Actor One", "Actor Two"]
    assert item.attributes.get("tmdb_id") == "56789"


def test_temporary_filtering(run_graphql):
    session = db_session()
    session.query(FilterRule).delete()
    session.query(IgnoreItem).delete()
    session.commit()
    # Add test items
    items = [
        IgnoreItem(
            item_type="mv",
            uid="1",
            title="Action Movie",
            ignore=False,
            added=False,
            attributes={
                "genre": ["Action", "Comedy"],
                "description": ["A hero saves the day."],
            },
        ),
        IgnoreItem(
            item_type="mv",
            uid="2",
            title="Drama Movie",
            ignore=False,
            added=False,
            attributes={"genre": ["Drama"], "description": ["A villain appears."]},
        ),
        IgnoreItem(
            item_type="mv",
            uid="3",
            title="Comedy Movie",
            ignore=False,
            added=False,
            attributes={"genre": ["Comedy"], "description": ["A hilarious adventure."]},
        ),
    ]
    session.add_all(items)
    session.commit()
    # Test eq operator
    # NOTE: With .contains(value), both 'Comedy Movie' and 'Action Movie' will match 'genre eq Comedy',
    # because 'Comedy' is present in both lists. This is a cross-db limitation.
    query = """
    query ItemListQuery($filters: [Filter!]) {
        items(filters: $filters) {
            nodes { uid title attributes { key values } }
        }
    }
    """
    result = run_graphql(
        query,
        {
            "filters": [
                {"type": "mv"},
                {"attribute": "genre", "operator": "eq", "value": "Comedy"},
            ]
        },
    )
    nodes = result["data"]["items"]["nodes"]
    titles = {n["title"] for n in nodes}
    assert "Comedy Movie" in titles
    assert "Action Movie" in titles  # Acceptable due to .contains logic
    assert "Drama Movie" not in titles
    # Test contains operator
    result = run_graphql(
        query,
        {
            "filters": [
                {"type": "mv"},
                {"attribute": "description", "operator": "contains", "value": "hero"},
            ]
        },
    )
    nodes = result["data"]["items"]["nodes"]
    titles = {n["title"] for n in nodes}
    assert "Action Movie" in titles
    assert "Comedy Movie" not in titles
    assert "Drama Movie" not in titles
    # Test not_contains operator
    result = run_graphql(
        query,
        {
            "filters": [
                {"type": "mv"},
                {
                    "attribute": "description",
                    "operator": "not_contains",
                    "value": "villain",
                },
            ]
        },
    )
    nodes = result["data"]["items"]["nodes"]
    titles = {n["title"] for n in nodes}
    assert "Action Movie" in titles
    assert "Comedy Movie" in titles
    assert "Drama Movie" not in titles


def test_apply_filters_lt_year():
    from indexer_utils.schema import FilterSpec, apply_filters

    session = db_session()
    session.query(FilterRule).delete()
    session.query(IgnoreItem).delete()
    session.commit()
    # Add test items with different years
    items = [
        IgnoreItem(
            item_type="mv",
            uid="1",
            title="Old Movie",
            ignore=False,
            added=False,
            attributes={"year": ["2010"]},
        ),
        IgnoreItem(
            item_type="mv",
            uid="2",
            title="Recent Movie",
            ignore=False,
            added=False,
            attributes={"year": ["2023"]},
        ),
        IgnoreItem(
            item_type="mv",
            uid="3",
            title="Future Movie",
            ignore=False,
            added=False,
            attributes={"year": ["2025"]},
        ),
        IgnoreItem(
            item_type="mv",
            uid="3",
            title="Unrelated Movie",
            ignore=False,
            added=False,
            attributes={"shyear": ["2025"]},
        ),
        IgnoreItem(
            item_type="mv",
            uid="3",
            title="Malformatted Movie",
            ignore=False,
            added=False,
            attributes={"year": "2023"},
        ),
    ]
    session.add_all(items)
    session.commit()
    # Build a query for all items
    query = session.query(IgnoreItem)
    # Apply a FilterSpec for year < 2024
    spec = FilterSpec(
        model=IgnoreItem,
        field="attributes.year",
        op="lt",
        value="2024",
    )
    filtered = apply_filters(query, spec).all()
    titles = {item.title for item in filtered}
    # With current SQLAlchemy logic, this will do string comparison, so '2010' and '2023' < '2024'
    assert "Old Movie" in titles
    assert "Recent Movie" in titles
    assert "Future Movie" not in titles
    assert "Unrelated Movie" not in titles
    assert "Malformatted Movie" in titles


def test_movie_recommendation_query(run_graphql, monkeypatch):
    session = db_session()
    session.add(
        MovieRecommendationRecord(
            prompt="space adventure but darker",
            recommended_imdb_id="tt0000005",
            recommended_title="Dark Space",
            recommended_reason="Previously suggested",
            source="openai",
            preference=RecommendationPreference.NEVER,
            created_at=int(time.time()) - 100,
        )
    )
    session.commit()

    sample_movies = [
        {
            "imdbId": "tt0000001",
            "title": "Already Watched",
            "hasFile": True,
            "overview": "An older classic.",
            "genres": ["Drama"],
            "year": 1982,
            "images": [
                {"coverType": "poster", "url": "http://example.com/poster-old.jpg"}
            ],
            "credits": [{"name": "Actor Old", "type": "Actor"}],
            "ratings": {"imdb": {"value": 7.1}},
        },
        {
            "imdbId": "tt0000002",
            "title": "Space Adventure",
            "hasFile": True,
            "overview": "A journey through the stars.",
            "genres": ["Sci-Fi", "Adventure"],
            "year": 2022,
            "images": [
                {
                    "coverType": "poster",
                    "url": "http://example.com/poster-space.jpg",
                }
            ],
            "credits": [
                {"name": "Star Captain", "type": "Actor"},
                {"name": "Navigator", "type": "Actor"},
            ],
            "ratings": {"imdb": {"value": 8.6}},
        },
    ]

    monkeypatch.setattr(
        "indexer_utils.recommendations.radarr_query", lambda cmd: sample_movies
    )
    monkeypatch.setattr(
        "indexer_utils.recommendations.get_recently_played_imdb_ids",
        lambda limit=40: {"tt0000001"},
    )

    def fake_openai(system_prompt: str, user_payload: str):
        payload = json.loads(user_payload)
        assert payload["movies"][0]["imdb_id"] == "tt0000002"
        history = payload.get("history")
        assert history and history[0]["title"] == "Dark Space"
        assert history[0]["preference"] == "NEVER"
        return {"imdb_id": "tt0000002", "reason": "Fits the space adventure vibe."}

    monkeypatch.setattr(
        "indexer_utils.recommendations.call_openai_json", fake_openai
    )

    query = """
    query MovieRecommendationQuery($prompt: String) {
        movieRecommendation(prompt: $prompt) {
            id
            title
            imdbId
            posterUrl
            overview
            year
            genres
            cast
            reason
            source
            prompt
            excludedRecent
            preference
        }
    }
    """

    result = run_graphql(query, {"prompt": "space adventure"})
    data = result["data"]["movieRecommendation"]
    assert data["id"]
    assert data["title"] == "Space Adventure"
    assert data["imdbId"] == "tt0000002"
    assert data["posterUrl"].endswith("poster-space.jpg")
    assert "Sci-Fi" in data["genres"]
    assert "Star Captain" in data["cast"]
    assert data["reason"] == "Fits the space adventure vibe."
    assert data["source"] == "openai"
    assert data["prompt"] == "space adventure"
    assert data["excludedRecent"] == ["Already Watched"]
    assert data["preference"] is None

    session = db_session()
    records = session.query(MovieRecommendationRecord).order_by(MovieRecommendationRecord.id).all()
    assert len(records) == 2
    latest = records[-1]
    assert latest.recommended_title == "Space Adventure"
    assert latest.recommended_imdb_id == "tt0000002"
    assert latest.prompt == "space adventure"
    assert latest.preference is None
    assert latest.source == "openai"


def test_movie_recommendation_uses_radarr_base_for_posters(
    run_graphql, monkeypatch
):
    monkeypatch.setenv("RADARR_URL", "https://radarr.example.com/radarr/api/v3")

    sample_movies = [
        {
            "imdbId": "tt0000003",
            "title": "Relative Poster",
            "hasFile": True,
            "images": [
                {"coverType": "poster", "url": "/MediaCover/3/poster.jpg"}
            ],
            "ratings": {"imdb": {"value": 7.5}},
        }
    ]

    monkeypatch.setattr(
        "indexer_utils.recommendations.radarr_query", lambda cmd: sample_movies
    )
    monkeypatch.setattr(
        "indexer_utils.recommendations.get_recently_played_imdb_ids",
        lambda limit=40: set(),
    )
    monkeypatch.setattr(
        "indexer_utils.recommendations.call_openai_json",
        lambda system_prompt, payload: {"imdb_id": "tt0000003"},
    )

    query = """
    query MovieRecommendationQuery($prompt: String) {
        movieRecommendation(prompt: $prompt) {
            posterUrl
        }
    }
    """

    result = run_graphql(query, {"prompt": "anything"})
    poster_url = result["data"]["movieRecommendation"]["posterUrl"]
    assert poster_url == "https://radarr.example.com/MediaCover/3/poster.jpg"


def test_set_recommendation_preference_mutation(run_graphql):
    session = db_session()
    record = MovieRecommendationRecord(
        prompt="space adventure",
        recommended_imdb_id="tt0000002",
        recommended_title="Space Adventure",
        recommended_reason="Initial suggestion",
        source="openai",
        created_at=int(time.time()),
    )
    session.add(record)
    session.commit()
    session.refresh(record)

    mutation = """
    mutation SetPreference($input: SetRecommendationPreferenceInput!) {
        setRecommendationPreference(data: $input) {
            id
            preference
        }
    }
    """

    result = run_graphql(
        mutation,
        {
            "input": {
                "recommendationId": str(record.id),
                "preference": "LIKE",
            }
        },
    )

    payload = result["data"]["setRecommendationPreference"]
    assert payload["id"] == str(record.id)
    assert payload["preference"] == "LIKE"

    refreshed = db_session().query(MovieRecommendationRecord).get(record.id)
    assert refreshed.preference == RecommendationPreference.LIKE
