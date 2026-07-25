"""get_watch_history: recent plays, title filtering, account mapping."""

from typing import Any, Dict, List

import pytest

import indexer_utils.plex_utils as plex_utils

MOVIE_PLAY = {
    "type": "movie",
    "title": "Heat",
    "year": 1995,
    "ratingKey": "101",
    "viewedAt": 300,
    "accountID": 1,
}
MOVIE_REWATCH = {
    "type": "movie",
    "title": "Heat",
    "year": 1995,
    "ratingKey": "101",
    "viewedAt": 100,
    "accountID": 2,
}
EPISODE_PLAY = {
    "type": "episode",
    "title": "Pilot",
    "grandparentTitle": "Severance",
    "parentIndex": 1,
    "index": 1,
    "ratingKey": "201",
    "grandparentKey": "/library/metadata/200",
    "viewedAt": 400,
    "accountID": 2,
}
DELETED_PLAY = {"type": "movie", "title": "Gone", "viewedAt": 250, "accountID": 1}
TRACK_PLAY = {"type": "track", "title": "Song", "viewedAt": 500, "accountID": 1}

HISTORY = [MOVIE_REWATCH, TRACK_PLAY, MOVIE_PLAY, DELETED_PLAY, EPISODE_PLAY]

ACCOUNTS = [{"id": 1, "name": "john"}, {"id": 2, "name": "kid"}]

HUBS_BY_QUERY = {
    "heat": [{"type": "movie", "Metadata": [{"ratingKey": "101"}]}],
    "severance": [{"type": "show", "Metadata": [{"ratingKey": "200"}]}],
}


def _patch_plex(monkeypatch: Any) -> List[Dict[str, Any]]:
    requests_seen: List[Dict[str, Any]] = []

    def fake_plex_get(path: str, **params: Any) -> Dict[str, Any]:
        requests_seen.append({"path": path, **params})
        if path == "/status/sessions/history/all":
            return {"Metadata": [dict(e) for e in HISTORY]}
        if path == "/accounts":
            return {"Account": ACCOUNTS}
        if path == "/hubs/search":
            return {"Hub": HUBS_BY_QUERY.get(params.get("query", ""), [])}
        raise AssertionError(f"unexpected Plex path {path}")

    monkeypatch.setattr(plex_utils, "_plex_get", fake_plex_get)
    return requests_seen


def test_recent_plays_sorted_and_video_only(monkeypatch):
    _patch_plex(monkeypatch)
    plays = plex_utils.get_watch_history(limit=10)
    assert [p["viewed_at"] for p in plays] == [400, 300, 250, 100]
    assert plays[0]["title"] == "Severance S1E1 - Pilot"
    assert plays[0]["item_type"] == "tv"
    assert plays[0]["account"] == "kid"
    assert plays[1]["account"] == "john"
    assert plays[1]["viewed_at_utc"] == "1970-01-01T00:05:00+00:00"
    assert "plex_rating_key" not in plays[2]


def test_limit_applies_after_filtering(monkeypatch):
    _patch_plex(monkeypatch)
    plays = plex_utils.get_watch_history(limit=2)
    assert [p["viewed_at"] for p in plays] == [400, 300]


def test_title_filters_to_movie_plays(monkeypatch):
    _patch_plex(monkeypatch)
    plays = plex_utils.get_watch_history(title="heat")
    assert [p["viewed_at"] for p in plays] == [300, 100]
    assert all(p["title"] == "Heat" for p in plays)


def test_title_matches_show_episodes_via_grandparent(monkeypatch):
    _patch_plex(monkeypatch)
    plays = plex_utils.get_watch_history(title="severance", item_type="tv")
    assert len(plays) == 1
    assert plays[0]["plex_rating_key"] == "201"


def test_item_type_narrows_server_side(monkeypatch):
    requests_seen = _patch_plex(monkeypatch)
    plex_utils.get_watch_history(item_type="mv")
    history_calls = [
        r for r in requests_seen if r["path"] == "/status/sessions/history/all"
    ]
    assert history_calls == [
        {"path": "/status/sessions/history/all", "metadataItemType": 1}
    ]


def test_unmatched_title_raises(monkeypatch):
    _patch_plex(monkeypatch)
    with pytest.raises(ValueError, match="nope"):
        plex_utils.get_watch_history(title="nope")


def test_bad_item_type_raises(monkeypatch):
    _patch_plex(monkeypatch)
    with pytest.raises(ValueError, match="item_type"):
        plex_utils.get_watch_history(item_type="movie")
