"""Tests for the video diagnostic/repair surface.

Two layers:

* The vdiag sidecar's ``safe_path`` — the only security-relevant pure function
  (path-root containment, symlink-escape rejection). Loaded straight from the
  service file since ``vdiag/`` is a standalone image, not an importable package.
* The MCP orchestration (``diagnose_video`` / ``repair_video``) and the arr
  re-download mapping, with the Plex/vdiag/arr edges mocked — asserting a Plex
  ratingKey resolves to the right path and the right downstream call fires.
"""

import importlib.util
import os
import pathlib
from typing import Any, Dict, List, Optional

import pytest
from fastapi import HTTPException

import indexer_utils.mcp_server as mcp_server
from indexer_utils import plex_utils, radarr_utils, sonarr_utils

_VDIAG_SERVER = pathlib.Path(__file__).resolve().parent.parent / "vdiag" / "server.py"
_spec = importlib.util.spec_from_file_location("vdiag_server", _VDIAG_SERVER)
assert _spec and _spec.loader
vserver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vserver)


# --------------------------------------------------------------------------
# sidecar path safety
# --------------------------------------------------------------------------


def test_safe_path_accepts_file_under_root(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    movie = root / "movie.mkv"
    movie.write_text("x")
    monkeypatch.setattr(vserver, "ALLOWED_ROOTS", [os.path.realpath(str(root))])
    assert vserver.safe_path(str(movie)) == os.path.realpath(str(movie))


def test_safe_path_rejects_outside_root(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    monkeypatch.setattr(vserver, "ALLOWED_ROOTS", [os.path.realpath(str(root))])
    with pytest.raises(HTTPException) as exc:
        vserver.safe_path(str(tmp_path / "elsewhere.mkv"), must_exist=False)
    assert exc.value.status_code == 400


def test_safe_path_rejects_symlink_escape(tmp_path, monkeypatch):
    root = tmp_path / "store"
    root.mkdir()
    secret = tmp_path / "secret.conf"
    secret.write_text("x")
    link = root / "innocent.mkv"
    link.symlink_to(secret)
    monkeypatch.setattr(vserver, "ALLOWED_ROOTS", [os.path.realpath(str(root))])
    with pytest.raises(HTTPException) as exc:
        vserver.safe_path(str(link))
    assert exc.value.status_code == 400


class _FakeResp:
    def __init__(self, status_code: int, payload: Dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Dict[str, Any]:
        return self._payload


def test_plex_file_path_returns_first_part(monkeypatch):
    payload = {
        "MediaContainer": {
            "Metadata": [{"Media": [{"Part": [{"file": "/store/x.mkv"}]}]}]
        }
    }
    monkeypatch.setattr(vserver, "PLEX_URL", "http://plex")
    monkeypatch.setattr(vserver, "PLEX_TOKEN", "t")
    monkeypatch.setattr(
        vserver.requests, "get", lambda *a, **k: _FakeResp(200, payload)
    )
    assert vserver.plex_file_path("62631") == "/store/x.mkv"


def test_plex_file_path_404_when_item_has_no_file(monkeypatch):
    payload = {"MediaContainer": {"Metadata": [{"Media": []}]}}
    monkeypatch.setattr(vserver, "PLEX_URL", "http://plex")
    monkeypatch.setattr(vserver, "PLEX_TOKEN", "t")
    monkeypatch.setattr(
        vserver.requests, "get", lambda *a, **k: _FakeResp(200, payload)
    )
    with pytest.raises(HTTPException) as exc:
        vserver.plex_file_path("62631")
    assert exc.value.status_code == 404


# --------------------------------------------------------------------------
# diagnose_video
# --------------------------------------------------------------------------


def _patch_resolve(monkeypatch, item: Optional[Dict[str, Any]]) -> None:
    async def fake_resolve(rating_key: str) -> Optional[Dict[str, Any]]:
        return item

    monkeypatch.setattr(mcp_server, "aresolve_item", fake_resolve)


async def test_diagnose_quick_probes_only(monkeypatch):
    _patch_resolve(monkeypatch, {"item_type": "mv", "title": "Foo"})
    calls: List[str] = []

    async def fake_probe(rating_key: str) -> Dict[str, Any]:
        calls.append(rating_key)
        return {"container": "matroska"}

    async def fake_scan(
        rating_key: str, duration: Optional[float] = None
    ) -> Dict[str, Any]:
        raise AssertionError("scan should not run for a quick diagnosis")

    monkeypatch.setattr(mcp_server, "aprobe", fake_probe)
    monkeypatch.setattr(mcp_server, "ascan", fake_scan)

    result = await mcp_server.diagnose_video("123")
    assert calls == ["123"]
    assert result["probe"] == {"container": "matroska"}
    assert "scan" not in result


async def test_diagnose_deep_also_scans(monkeypatch):
    _patch_resolve(monkeypatch, {"item_type": "mv", "title": "Foo"})

    async def fake_probe(rating_key: str) -> Dict[str, Any]:
        return {"ok": True}

    scanned: List[str] = []

    async def fake_scan(
        rating_key: str, duration: Optional[float] = None
    ) -> Dict[str, Any]:
        scanned.append(rating_key)
        return {"ok": False, "error_count": 9}

    monkeypatch.setattr(mcp_server, "aprobe", fake_probe)
    monkeypatch.setattr(mcp_server, "ascan", fake_scan)

    result = await mcp_server.diagnose_video("123", deep=True)
    assert scanned == ["123"]
    assert result["scan"]["error_count"] == 9


async def test_diagnose_raises_for_unknown_rating_key(monkeypatch):
    _patch_resolve(monkeypatch, None)
    with pytest.raises(ValueError):
        await mcp_server.diagnose_video("123")


# --------------------------------------------------------------------------
# repair_video
# --------------------------------------------------------------------------


async def test_repair_remux_then_refreshes_plex(monkeypatch):
    _patch_resolve(monkeypatch, {"item_type": "mv", "title": "Foo"})
    remuxed: List[str] = []
    refreshed: List[str] = []

    async def fake_remux(rating_key: str) -> Dict[str, Any]:
        remuxed.append(rating_key)
        return {"remuxed": True}

    async def fake_refresh(rating_key: str) -> None:
        refreshed.append(rating_key)

    monkeypatch.setattr(mcp_server, "aremux", fake_remux)
    monkeypatch.setattr(mcp_server, "arefresh_item", fake_refresh)

    result = await mcp_server.repair_video("123", "remux")
    assert remuxed == ["123"]
    assert refreshed == ["123"]
    assert result["mode"] == "remux"


async def test_repair_redownload_movie_uses_imdb(monkeypatch):
    _patch_resolve(
        monkeypatch, {"item_type": "mv", "title": "Foo", "imdb_id": "tt0099"}
    )
    seen: Dict[str, Any] = {}

    async def fake_redownload(imdb_id: str) -> Dict[str, Any]:
        seen["imdb"] = imdb_id
        return {"status": "searching"}

    monkeypatch.setattr(mcp_server, "aredownload_by_imdb", fake_redownload)

    result = await mcp_server.repair_video("123", "redownload")
    assert seen["imdb"] == "tt0099"
    assert result["mode"] == "redownload"


async def test_repair_redownload_episode_uses_tvdb_season_episode(monkeypatch):
    _patch_resolve(
        monkeypatch,
        {
            "item_type": "tv",
            "title": "Foo S1E2",
            "tvdb_id": "555",
            "season": 1,
            "episode": 2,
        },
    )
    seen: Dict[str, Any] = {}

    async def fake_redownload(
        tvdb_id: str, season: int, episode: int
    ) -> Dict[str, Any]:
        seen.update(tvdb=tvdb_id, season=season, episode=episode)
        return {"status": "searching"}

    monkeypatch.setattr(mcp_server, "aredownload_episode", fake_redownload)

    await mcp_server.repair_video("123", "redownload")
    assert seen == {"tvdb": "555", "season": 1, "episode": 2}


async def test_repair_rejects_unknown_mode(monkeypatch):
    _patch_resolve(monkeypatch, {"item_type": "mv", "file_path": "/store/Foo.mkv"})
    with pytest.raises(ValueError):
        await mcp_server.repair_video("123", "transcode")


# --------------------------------------------------------------------------
# arr re-download mapping (the real path-to-id logic)
# --------------------------------------------------------------------------


async def test_aredownload_by_imdb_deletes_then_searches(monkeypatch):
    async def fake_get_movie(imdb_id: str) -> Dict[str, Any]:
        return {"id": 7, "movieFile": {"id": 42}}

    calls: List[Dict[str, Any]] = []

    async def fake_query(cmd: str, method: str = "get", **kwargs: Any) -> List[Any]:
        calls.append({"cmd": cmd, "method": method, "kwargs": kwargs})
        return []

    reset: List[bool] = []
    monkeypatch.setattr(radarr_utils, "aget_movie", fake_get_movie)
    monkeypatch.setattr(radarr_utils, "aradarr_query", fake_query)
    monkeypatch.setattr(radarr_utils, "reset_movies", lambda: reset.append(True))

    result = await radarr_utils.aredownload_by_imdb("tt0099")
    assert calls[0] == {"cmd": "moviefile/42", "method": "delete", "kwargs": {}}
    assert calls[1]["cmd"] == "command"
    assert calls[1]["kwargs"]["name"] == "MoviesSearch"
    assert calls[1]["kwargs"]["movieIds"] == [7]
    assert result["deleted_old_file"] is True
    assert reset == [True]


def test_now_playing_formats_episode_and_session(monkeypatch):
    session = {
        "Metadata": [
            {
                "type": "episode",
                "ratingKey": 62631,
                "grandparentTitle": "Psych",
                "parentIndex": 8,
                "index": 2,
                "title": "S.E.I.Z.E. the Day",
                "Media": [
                    {"Part": [{"file": "/mnt/syno1/TV/Psych/s08e02.mp4", "size": 5}]}
                ],
                "Player": {"title": "Apple TV", "state": "playing"},
                "User": {"title": "johnfink8"},
            }
        ]
    }
    monkeypatch.setattr(plex_utils, "_plex_get", lambda path, **kw: session)

    rows = plex_utils.now_playing()
    assert len(rows) == 1
    row = rows[0]
    assert row["item_type"] == "tv"
    assert row["title"] == "Psych S8E2 - S.E.I.Z.E. the Day"
    assert row["plex_rating_key"] == "62631"
    assert row["file_path"] == "/mnt/syno1/TV/Psych/s08e02.mp4"
    assert row["player"] == "Apple TV"
    assert row["state"] == "playing"
    assert row["user"] == "johnfink8"


async def test_aredownload_episode_matches_season_and_number(monkeypatch):
    async def fake_get_series(tvdb_id: int) -> Dict[str, Any]:
        return {"id": 3}

    async def fake_query(cmd: str, post: bool = False, **kwargs: Any) -> Any:
        if cmd == "episode" and not post:
            return [
                {"id": 10, "seasonNumber": 1, "episodeNumber": 1, "episodeFileId": 0},
                {"id": 11, "seasonNumber": 1, "episodeNumber": 2, "episodeFileId": 99},
            ]
        if cmd.startswith("episode/"):
            return {"episodeFileId": 99}
        return None

    deletes: List[str] = []

    async def fake_delete(cmd: str, **kwargs: Any) -> Any:
        deletes.append(cmd)
        return None

    monkeypatch.setattr(sonarr_utils, "aget_series", fake_get_series)
    monkeypatch.setattr(sonarr_utils, "asn_query", fake_query)
    monkeypatch.setattr(sonarr_utils, "asn_delete", fake_delete)

    result = await sonarr_utils.aredownload_episode("555", 1, 2)
    assert result["episode_id"] == 11
    assert deletes == ["episodefile/99"]


# --------------------------------------------------------------------------
# quality upgrades
# --------------------------------------------------------------------------


async def test_aupgrade_by_imdb_switches_profile_then_searches(monkeypatch):
    async def fake_get_movie(imdb_id: str) -> Dict[str, Any]:
        return {"id": 7, "qualityProfileId": 1}

    calls: List[Dict[str, Any]] = []

    async def fake_query(cmd: str, method: str = "get", **kwargs: Any) -> Any:
        calls.append({"cmd": cmd, "method": method, "kwargs": kwargs})
        if cmd == "movie/7" and method == "get":
            return {"id": 7, "qualityProfileId": 1}
        return []

    monkeypatch.setattr(radarr_utils, "aget_movie", fake_get_movie)
    monkeypatch.setattr(radarr_utils, "aradarr_query", fake_query)

    result = await radarr_utils.aupgrade_by_imdb("tt0099", 4)
    put = next(c for c in calls if c["method"] == "put")
    assert put["cmd"] == "movie/7"
    assert put["kwargs"]["qualityProfileId"] == 4
    search = next(c for c in calls if c["kwargs"].get("name") == "MoviesSearch")
    assert search["kwargs"]["movieIds"] == [7]
    assert result["quality_profile_id"] == 4


async def test_aupgrade_by_tvdb_sets_series_profile_and_searches_episode(monkeypatch):
    async def fake_get_series(tvdb_id: int) -> Dict[str, Any]:
        return {"id": 3}

    async def fake_query(cmd: str, post: bool = False, **kwargs: Any) -> Any:
        if cmd == "episode":
            return [{"id": 11, "seasonNumber": 1, "episodeNumber": 2}]
        if cmd == "series/3":
            return {"id": 3, "qualityProfileId": 1}
        return None

    puts: List[Dict[str, Any]] = []
    commands: List[Dict[str, Any]] = []

    async def fake_put(cmd: str, **kwargs: Any) -> Any:
        puts.append({"cmd": cmd, "kwargs": kwargs})
        return None

    async def fake_query_recording(cmd: str, post: bool = False, **kwargs: Any) -> Any:
        if cmd == "command":
            commands.append(kwargs)
            return None
        return await fake_query(cmd, post, **kwargs)

    monkeypatch.setattr(sonarr_utils, "aget_series", fake_get_series)
    monkeypatch.setattr(sonarr_utils, "asn_query", fake_query_recording)
    monkeypatch.setattr(sonarr_utils, "asn_put", fake_put)

    result = await sonarr_utils.aupgrade_by_tvdb("555", 5, season=1, episode=2)
    assert puts[0]["cmd"] == "series/3"
    assert puts[0]["kwargs"]["qualityProfileId"] == 5
    assert commands[0]["name"] == "EpisodeSearch"
    assert commands[0]["episodeIds"] == [11]
    assert result["episode_id"] == 11


async def test_upgrade_video_preview_lists_movie_profiles(monkeypatch):
    _patch_resolve(
        monkeypatch, {"item_type": "mv", "title": "Foo", "imdb_id": "tt0099"}
    )

    async def fake_get_movie(imdb_id: str) -> Dict[str, Any]:
        return {"qualityProfileId": 1}

    async def fake_query(cmd: str, method: str = "get", **kwargs: Any) -> Any:
        return [{"id": 1, "name": "SD"}, {"id": 4, "name": "HD-1080p"}]

    monkeypatch.setattr(mcp_server, "aget_movie", fake_get_movie)
    monkeypatch.setattr(mcp_server, "aradarr_query", fake_query)

    result = await mcp_server.upgrade_video("123")
    assert result["item_type"] == "mv"
    assert result["current_quality_profile_id"] == 1
    assert {"id": 4, "name": "HD-1080p"} in result["available_profiles"]


async def test_upgrade_video_apply_tv_routes_to_sonarr(monkeypatch):
    _patch_resolve(
        monkeypatch,
        {
            "item_type": "tv",
            "title": "Foo",
            "tvdb_id": "555",
            "season": 1,
            "episode": 2,
        },
    )
    seen: Dict[str, Any] = {}

    async def fake_upgrade(
        tvdb_id: str,
        quality_profile_id: Optional[int] = None,
        season: Optional[int] = None,
        episode: Optional[int] = None,
    ) -> Dict[str, Any]:
        seen.update(
            tvdb=tvdb_id, profile=quality_profile_id, season=season, episode=episode
        )
        return {"status": "searching"}

    monkeypatch.setattr(mcp_server, "aupgrade_by_tvdb", fake_upgrade)

    await mcp_server.upgrade_video("123", 5)
    assert seen == {"tvdb": "555", "profile": 5, "season": 1, "episode": 2}
