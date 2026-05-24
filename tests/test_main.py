# flake8: noqa: ANN001,ANN101,ANN201

import base64
import json
import logging
import time
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select

from indexer_utils import main
from indexer_utils.filters import should_ignore_by_rules
from indexer_utils.models import (
    FilterRule,
    IgnoreItem,
    MovieRecommendationRecord,
    RecommendationPreference,
)
from indexer_utils.session import db_session
from indexer_utils.vid_utils import check_movies, check_shows

logging.getLogger().setLevel(logging.DEBUG)


@pytest_asyncio.fixture
async def session():
    """Per-test async session against the test postgres."""
    async with db_session() as s:
        yield s


@pytest.fixture
def client_and_db(monkeypatch):
    # Stub Radarr/Sonarr add calls so add_item mutations don't try to talk
    # to real services.
    monkeypatch.setattr("indexer_utils.schema.addMovie", lambda uid: None)
    monkeypatch.setattr("indexer_utils.schema.add_series", lambda uid: None)
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


def test_check_new_and_titles(monkeypatch):
    from indexer_utils import jobs

    calls: list = []

    async def afake_check_movies(days):
        calls.append(("check_movies", days))

    async def afake_check_shows(days):
        calls.append(("check_shows", days))

    async def afake_get_movie_titles():
        calls.append(("get_movie_titles",))

    async def afake_get_show_titles():
        calls.append(("get_show_titles",))

    monkeypatch.setattr(jobs, "check_movies", afake_check_movies)
    monkeypatch.setattr(jobs, "check_shows", afake_check_shows)
    monkeypatch.setattr(jobs, "get_movie_titles", afake_get_movie_titles)
    monkeypatch.setattr(jobs, "get_show_titles", afake_get_show_titles)
    monkeypatch.setattr(jobs, "_signal_event", lambda item_type: None)

    jobs.run_check_new_items()
    expected_movie_calls = [
        ("check_movies", 1),
        ("check_movies", 4),
        ("check_movies", 30),
    ]
    expected_show_calls = [
        ("check_shows", 1),
        ("check_shows", 4),
        ("check_shows", 30),
    ]
    assert calls[:3] == expected_movie_calls
    assert calls[3:6] == expected_show_calls

    calls.clear()
    jobs.run_check_titles()
    assert ("get_movie_titles",) in calls
    assert ("get_show_titles",) in calls


def test_index_returns_html(client_and_db):
    client, _ = client_and_db
    res_index = client.get("/")
    assert res_index.status_code == 200
    assert "text/html" in res_index.headers["content-type"]
    html = res_index.text
    assert "<script" in html or "<link" in html


def test_query_items(run_graphql):
    query = """
    query ItemListQuery($itemType: String) {
        items(itemType: $itemType) {
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
    result = run_graphql(query, {"itemType": "mv"})
    assert "data" in result
    assert "items" in result["data"]
    assert "nodes" in result["data"]["items"]


async def test_add_and_delete_item(run_graphql, session):
    item = IgnoreItem(
        item_type="mv",
        uid="testuid",
        title="Test Movie",
        ignore=False,
        added=False,
        attributes={},
    )
    session.add(item)
    await session.commit()
    item_id = item.id

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


async def test_query_items_filtering(run_graphql, session):
    items = [
        IgnoreItem(
            item_type="mv",
            uid="uid1",
            title="Action Movie",
            ignore=False,
            added=False,
            attributes={"genre": ["Action"]},
        ),
        IgnoreItem(
            item_type="mv",
            uid="uid2",
            title="Drama Movie",
            ignore=False,
            added=False,
            attributes={"genre": ["Drama"]},
        ),
        IgnoreItem(
            item_type="mv",
            uid="uid3",
            title="Comedy Movie",
            ignore=False,
            added=False,
            attributes={"genre": ["Comedy"]},
        ),
    ]
    for it in items:
        session.add(it)
    await session.commit()

    query = """
    query ItemListQuery($itemType: String) {
        items(itemType: $itemType) {
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
    result = run_graphql(query, {"itemType": "mv"})
    assert "data" in result
    nodes = result["data"]["items"]["nodes"]
    titles = {n["title"] for n in nodes}
    assert "Drama Movie" in titles
    assert "Action Movie" in titles
    assert "Comedy Movie" in titles


async def test_historical_items_pagination(run_graphql, session):
    now = int(time.time())
    for i in range(5):
        session.add(
            IgnoreItem(
                item_type="mv" if i % 2 == 0 else "tv",
                uid=f"ignored-{i}",
                title=f"Ignored {i}",
                ignore=True,
                added=False,
                created_at=now - i,
                attributes={},
            )
        )
    for i in range(3):
        session.add(
            IgnoreItem(
                item_type="mv",
                uid=f"notignored-{i}",
                title=f"NotIgnored {i}",
                ignore=False,
                added=False,
                created_at=now - 10 - i,
                attributes={},
            )
        )
    await session.commit()

    query = """
    query HistoricalItems($limit: Int!, $offset: Int!) {
        historicalItems(limit: $limit, offset: $offset) {
            nodes { id type uid title ignore createdAt: attributes { key values } }
            pageInfo { hasNextPage hasPreviousPage startOffset endOffset totalCount }
        }
    }
    """
    result = run_graphql(query, {"limit": 2, "offset": 0})
    nodes = result["data"]["historicalItems"]["nodes"]
    assert len(nodes) == 2
    for n in nodes:
        assert n["uid"].startswith("ignored-")
    page_info = result["data"]["historicalItems"]["pageInfo"]
    assert page_info["hasNextPage"] is True
    assert page_info["startOffset"] == 0
    assert page_info["endOffset"] == 1

    result2 = run_graphql(query, {"limit": 2, "offset": 2})
    nodes2 = result2["data"]["historicalItems"]["nodes"]
    assert len(nodes2) == 2
    for n in nodes2:
        assert n["uid"].startswith("ignored-")
    page_info2 = result2["data"]["historicalItems"]["pageInfo"]
    assert page_info2["hasNextPage"] is True
    assert page_info2["startOffset"] == 2
    assert page_info2["endOffset"] == 3

    result3 = run_graphql(query, {"limit": 2, "offset": 4})
    nodes3 = result3["data"]["historicalItems"]["nodes"]
    for n in nodes3:
        assert n["uid"].startswith("ignored-")
    page_info3 = result3["data"]["historicalItems"]["pageInfo"]
    assert page_info3["hasNextPage"] is False
    assert page_info3["startOffset"] == 4
    assert page_info3["endOffset"] in (4, 5)
    all_uids = [n["uid"] for n in nodes + nodes2 + nodes3]
    assert all(uid.startswith("ignored-") for uid in all_uids)


async def _add_rule(session, **kwargs):
    defaults = dict(item_type="mv", enabled=True)
    defaults.update(kwargs)
    session.add(FilterRule(**defaults))
    await session.commit()


async def test_should_ignore_eq(session):
    await _add_rule(session, attribute="genre", operator="eq", value="Action")
    item = IgnoreItem(
        item_type="mv", uid="1", attributes={"genre": ["Action", "Comedy"]}
    )
    assert await should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"genre": ["Drama"]})
    assert await should_ignore_by_rules(item) is False


async def test_should_ignore_neq(session):
    await _add_rule(session, attribute="lang", operator="neq", value="French")
    item = IgnoreItem(item_type="mv", uid="1", attributes={"lang": ["English"]})
    assert await should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"lang": ["French"]})
    assert await should_ignore_by_rules(item) is False


async def test_should_ignore_lt(session):
    await _add_rule(session, attribute="year", operator="lt", value="2000")
    item = IgnoreItem(item_type="mv", uid="1", attributes={"year": ["1999"]})
    assert await should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"year": ["2001"]})
    assert await should_ignore_by_rules(item) is False


async def test_should_ignore_gt(session):
    await _add_rule(session, attribute="rating", operator="gt", value="7.0")
    item = IgnoreItem(item_type="mv", uid="1", attributes={"rating": ["8.0"]})
    assert await should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"rating": ["6.0"]})
    assert await should_ignore_by_rules(item) is False


async def test_should_ignore_lte(session):
    await _add_rule(session, attribute="votes", operator="lte", value="1000")
    item = IgnoreItem(item_type="mv", uid="1", attributes={"votes": ["1000"]})
    assert await should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"votes": ["1001"]})
    assert await should_ignore_by_rules(item) is False


async def test_should_ignore_gte(session):
    await _add_rule(session, attribute="runtime", operator="gte", value="120")
    item = IgnoreItem(item_type="mv", uid="1", attributes={"runtime": ["130"]})
    assert await should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"runtime": ["110"]})
    assert await should_ignore_by_rules(item) is False


async def test_should_ignore_in(session):
    await _add_rule(session, attribute="country", operator="in", value="USA")
    item = IgnoreItem(item_type="mv", uid="1", attributes={"country": ["USA", "UK"]})
    assert await should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"country": ["Canada"]})
    assert await should_ignore_by_rules(item) is False


async def test_should_ignore_notin(session):
    await _add_rule(session, attribute="country", operator="notin", value="Canada")
    item = IgnoreItem(item_type="mv", uid="1", attributes={"country": ["USA", "UK"]})
    assert await should_ignore_by_rules(item) is True
    item = IgnoreItem(item_type="mv", uid="2", attributes={"country": ["Canada"]})
    assert await should_ignore_by_rules(item) is False


async def test_should_ignore_contains(session):
    await _add_rule(session, attribute="description", operator="contains", value="hero")
    item = IgnoreItem(
        item_type="mv",
        uid="1",
        attributes={"description": ["A hero saves the day."]},
    )
    assert await should_ignore_by_rules(item) is True
    item = IgnoreItem(
        item_type="mv", uid="2", attributes={"description": ["A villain appears."]}
    )
    assert await should_ignore_by_rules(item) is False


async def test_should_ignore_not_contains(session):
    await _add_rule(
        session, attribute="description", operator="not_contains", value="villain"
    )
    item = IgnoreItem(
        item_type="mv",
        uid="1",
        attributes={"description": ["A hero saves the day."]},
    )
    assert await should_ignore_by_rules(item) is True
    item = IgnoreItem(
        item_type="mv", uid="2", attributes={"description": ["A villain appears."]}
    )
    assert await should_ignore_by_rules(item) is False


async def test_check_movies_creates_ignore_items(monkeypatch, session):
    rule = FilterRule(
        item_type="mv",
        attribute="genres",
        operator="eq",
        value="Comedy",
        enabled=True,
    )
    session.add(rule)
    await session.commit()

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

    monkeypatch.setattr(
        "requests.get", lambda url, params=None, **kwargs: MockResponse(xml)
    )

    def mock_radarr_query(endpoint, method=None, **kwargs):
        if endpoint == "movie/lookup":
            return [
                {
                    "year": "2022",
                    "title": "Test Movie",
                    "tmdbId": "999",
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
    monkeypatch.setattr("indexer_utils.vid_utils.get_movie", lambda imdb_id: None)
    monkeypatch.setattr("indexer_utils.vid_utils.find_movie", lambda title, year: None)
    monkeypatch.setattr("indexer_utils.vid_utils.reset_movies", lambda: None)

    await check_movies(days=1)

    async with db_session() as s:
        items = list((await s.execute(select(IgnoreItem))).scalars())
    assert len(items) == 1
    item = items[0]
    assert item.title == "Test Movie"
    assert item.uid == "tt1234567"
    assert item.ignore is True
    assert item.item_type == "mv"
    assert item.attributes is not None
    assert "genres" in item.attributes
    assert "Comedy" in item.attributes["genres"]


async def test_check_shows_fetches_cast(monkeypatch):
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

    monkeypatch.setattr(
        "requests.get", lambda url, params=None, **kwargs: MockResponse(xml)
    )

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

    async def mock_ignore_by_rules(item):
        return False

    monkeypatch.setattr(
        "indexer_utils.vid_utils.should_ignore_by_rules", mock_ignore_by_rules
    )

    def mock_get_tv_cast(tmdb_id, n=10):
        assert tmdb_id == "56789"
        return ["Actor One", "Actor Two"]

    monkeypatch.setattr("indexer_utils.vid_utils.get_tv_cast", mock_get_tv_cast)
    monkeypatch.setattr("indexer_utils.vid_utils.get_tv_id", lambda tvdb: "56789")

    async def mock_annotate(item_type, uid, title, attrs, **kwargs):
        assert attrs["cast"] == ["Actor One", "Actor Two"]
        return attrs

    monkeypatch.setattr("indexer_utils.vid_utils.annotate_with_ai_async", mock_annotate)

    await check_shows(days=1)

    async with db_session() as s:
        items = list((await s.execute(select(IgnoreItem))).scalars())
    assert len(items) == 1
    item = items[0]
    assert item.item_type == "tv"
    assert item.attributes is not None
    assert item.attributes.get("cast") == ["Actor One", "Actor Two"]
    assert item.attributes.get("tmdb_id") == "56789"
    assert item.shown is True


async def test_get_open_excludes_deferred_items(session):
    session.add_all(
        [
            IgnoreItem(
                item_type="mv", uid="1", title="Visible", ignore=False, shown=True
            ),
            IgnoreItem(
                item_type="mv",
                uid="2",
                title="Deferred",
                ignore=False,
                shown=True,
                defer_until=datetime.utcnow() + timedelta(days=2),
            ),
            IgnoreItem(
                item_type="mv",
                uid="3",
                title="Due",
                ignore=False,
                shown=True,
                defer_until=datetime.utcnow() - timedelta(days=1),
            ),
        ]
    )
    await session.commit()

    open_items = await IgnoreItem.get_open()
    titles = {item.title for item in open_items}
    assert "Visible" in titles
    assert "Due" in titles
    assert "Deferred" not in titles


async def test_movie_recommendation_query(run_graphql, monkeypatch, session):
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
    await session.commit()

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
        return (
            {"imdb_id": "tt0000002", "reason": "Fits the space adventure vibe."},
            None,
        )

    monkeypatch.setattr("indexer_utils.recommendations.call_openai_json", fake_openai)

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

    async with db_session() as s:
        records = list(
            (
                await s.execute(
                    select(MovieRecommendationRecord).order_by(
                        MovieRecommendationRecord.id
                    )
                )
            ).scalars()
        )
    assert len(records) == 2
    latest = records[-1]
    assert latest.recommended_title == "Space Adventure"
    assert latest.recommended_imdb_id == "tt0000002"
    assert latest.prompt == "space adventure"
    assert latest.preference is None
    assert latest.source == "openai"


def test_movie_recommendation_uses_radarr_base_for_posters(run_graphql, monkeypatch):
    monkeypatch.setenv("RADARR_URL", "https://radarr.example.com/radarr/api/v3")

    sample_movies = [
        {
            "imdbId": "tt0000003",
            "title": "Relative Poster",
            "hasFile": True,
            "images": [{"coverType": "poster", "url": "/MediaCover/3/poster.jpg"}],
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
        lambda system_prompt, payload: ({"imdb_id": "tt0000003"}, None),
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


async def test_set_recommendation_preference_mutation(run_graphql, session):
    record = MovieRecommendationRecord(
        prompt="space adventure",
        recommended_imdb_id="tt0000002",
        recommended_title="Space Adventure",
        recommended_reason="Initial suggestion",
        source="openai",
        created_at=int(time.time()),
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    record_id = record.id

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
                "recommendationId": str(record_id),
                "preference": "LIKE",
            }
        },
    )

    payload = result["data"]["setRecommendationPreference"]
    assert payload["id"] == str(record_id)
    assert payload["preference"] == "LIKE"

    async with db_session() as s:
        refreshed = await s.get(MovieRecommendationRecord, record_id)
    assert refreshed.preference == RecommendationPreference.LIKE
