"""Refetch paths must leave the item monitored.

A forced Radarr/Sonarr search grabs regardless of the monitored flag, but a
search that finds nothing strands an unmonitored item — RSS never picks it up
when a release appears later.
"""

from typing import Any, Dict, List, Tuple

import indexer_utils.radarr_utils as radarr_utils
import indexer_utils.sonarr_utils as sonarr_utils

Call = Tuple[str, str, Dict[str, Any]]


def _patch_radarr(monkeypatch: Any, movie: Dict[str, Any]) -> List[Call]:
    calls: List[Call] = []

    def fake_query(cmd: str, method: str = "get", **kwargs: Any) -> Any:
        calls.append((cmd, method, kwargs))
        if method == "get":
            return dict(movie)
        return {}

    monkeypatch.setattr(radarr_utils, "radarr_query", fake_query)
    return calls


def _puts(calls: List[Call]) -> List[Call]:
    return [c for c in calls if c[1] == "put"]


async def test_upgrade_remonitors_unmonitored_movie(monkeypatch):
    calls = _patch_radarr(monkeypatch, {"id": 5, "monitored": False})
    await radarr_utils.aupgrade_movie(5)
    puts = _puts(calls)
    assert len(puts) == 1
    assert puts[0][0] == "movie/5"
    assert puts[0][2]["monitored"] is True
    assert calls[-1] == (
        "command",
        "post",
        {"name": "MoviesSearch", "movieIds": [5]},
    )


async def test_upgrade_skips_put_when_already_monitored(monkeypatch):
    calls = _patch_radarr(monkeypatch, {"id": 5, "monitored": True})
    await radarr_utils.aupgrade_movie(5)
    assert not _puts(calls)
    assert calls[-1][0] == "command"


async def test_upgrade_with_profile_sets_profile_and_monitored(monkeypatch):
    calls = _patch_radarr(
        monkeypatch, {"id": 5, "monitored": True, "qualityProfileId": 1}
    )
    await radarr_utils.aupgrade_movie(5, quality_profile_id=9)
    puts = _puts(calls)
    assert len(puts) == 1
    assert puts[0][2]["qualityProfileId"] == 9
    assert puts[0][2]["monitored"] is True


async def test_redownload_remonitors_before_search(monkeypatch):
    movie = {"id": 7, "monitored": False, "movieFile": {"id": 3}}

    async def fake_get_movie(imdb_id: str) -> Dict[str, Any]:
        return dict(movie)

    monkeypatch.setattr(radarr_utils, "aget_movie", fake_get_movie)
    calls = _patch_radarr(monkeypatch, movie)
    result = await radarr_utils.aredownload_by_imdb("tt1")
    assert result["deleted_old_file"] is True
    assert ("moviefile/3", "delete", {}) in calls
    puts = _puts(calls)
    assert len(puts) == 1
    assert puts[0][0] == "movie/7"
    assert puts[0][2]["monitored"] is True
    assert calls.index(puts[0]) < calls.index(calls[-1])
    assert calls[-1][0] == "command"


async def test_redownload_skips_put_when_monitored(monkeypatch):
    movie = {"id": 7, "monitored": True, "movieFile": {}}

    async def fake_get_movie(imdb_id: str) -> Dict[str, Any]:
        return dict(movie)

    monkeypatch.setattr(radarr_utils, "aget_movie", fake_get_movie)
    calls = _patch_radarr(monkeypatch, movie)
    result = await radarr_utils.aredownload_by_imdb("tt1")
    assert result["deleted_old_file"] is False
    assert not _puts(calls)


def _patch_sonarr(monkeypatch: Any, episode: Dict[str, Any]) -> List[Call]:
    calls: List[Call] = []

    def fake_sn_query(cmd: str, post: bool = False, **kwargs: Any) -> Any:
        calls.append((cmd, "post" if post else "get", kwargs))
        if cmd == f"episode/{episode['id']}":
            return dict(episode)
        return {}

    def fake_request(cmd: str, method: str, **kwargs: Any) -> Any:
        calls.append((cmd, method, kwargs))
        return {}

    monkeypatch.setattr(sonarr_utils, "sn_query", fake_sn_query)
    monkeypatch.setattr(sonarr_utils, "_sn_request", fake_request)
    return calls


async def test_regrab_episode_remonitors(monkeypatch):
    calls = _patch_sonarr(
        monkeypatch, {"id": 11, "episodeFileId": 4, "monitored": False}
    )
    result = await sonarr_utils.aregrab_episode(11)
    assert result["deleted_old_file"] is True
    assert ("episodefile/4", "delete", {}) in calls
    puts = _puts(calls)
    assert len(puts) == 1
    assert puts[0][0] == "episode/11"
    assert puts[0][2]["monitored"] is True
    assert calls[-1][0] == "command"


async def test_regrab_episode_skips_put_when_monitored(monkeypatch):
    calls = _patch_sonarr(
        monkeypatch, {"id": 11, "episodeFileId": 0, "monitored": True}
    )
    result = await sonarr_utils.aregrab_episode(11)
    assert result["deleted_old_file"] is False
    assert not _puts(calls)
