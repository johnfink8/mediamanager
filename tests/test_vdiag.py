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
from fastmcp.exceptions import ToolError

import indexer_utils.mcp_server as mcp_server
from indexer_utils import plex_utils, radarr_utils, sonarr_utils
from indexer_utils.compat import assess_compatibility
from indexer_utils.vdiag_client import VdiagError

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
# sidecar background jobs
# --------------------------------------------------------------------------


def test_progress_from_line_parses_out_time():
    assert vserver._progress_from_line("out_time_us=30000000", 60.0) == 50.0
    # caps at 99 even once ffmpeg passes the probed duration
    assert vserver._progress_from_line("out_time_us=99000000", 60.0) == 99.0
    # ignored: non-progress lines, N/A, and a missing total
    assert vserver._progress_from_line("frame=10", 60.0) is None
    assert vserver._progress_from_line("out_time_us=N/A", 60.0) is None
    assert vserver._progress_from_line("out_time_us=30000000", None) is None


def test_run_scan_marks_job_done_clean(monkeypatch):
    monkeypatch.setattr(vserver, "_write_job", lambda job: None)
    monkeypatch.setattr(vserver, "_probe_duration", lambda path: 100.0)
    monkeypatch.setattr(vserver, "_run_ffmpeg_progress", lambda cmd, total, cb: (0, ""))
    job = {"id": "j1", "kind": "scan", "rating_key": "1", "status": "running"}
    vserver._run_scan(job, "/store/x.mkv", None)
    assert job["status"] == "done"
    assert job["result"]["ok"] is True
    assert job["result"]["error_count"] == 0


def test_run_scan_reports_decode_errors(monkeypatch):
    monkeypatch.setattr(vserver, "_write_job", lambda job: None)
    monkeypatch.setattr(vserver, "_probe_duration", lambda path: 100.0)
    monkeypatch.setattr(
        vserver, "_run_ffmpeg_progress", lambda cmd, total, cb: (0, "err one\nerr two")
    )
    job = {"id": "j2", "kind": "scan", "rating_key": "1", "status": "running"}
    vserver._run_scan(job, "/store/x.mkv", None)
    assert job["status"] == "done"
    assert job["result"]["ok"] is False
    assert job["result"]["error_count"] == 2


def test_run_scan_failure_marks_job_error(monkeypatch):
    monkeypatch.setattr(vserver, "_write_job", lambda job: None)

    def boom(path):
        raise RuntimeError("probe blew up")

    monkeypatch.setattr(vserver, "_probe_duration", boom)
    job = {"id": "j3", "kind": "scan", "rating_key": "1", "status": "running"}
    vserver._run_scan(job, "/store/x.mkv", None)
    assert job["status"] == "error"
    assert "probe blew up" in job["error"]


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

    async def fake_start_scan(
        rating_key: str, duration: Optional[float] = None
    ) -> Dict[str, Any]:
        raise AssertionError("a quick diagnosis must not start a scan job")

    monkeypatch.setattr(mcp_server, "aprobe", fake_probe)
    monkeypatch.setattr(mcp_server, "astart_scan", fake_start_scan)

    result = await mcp_server.diagnose_video("123")
    assert calls == ["123"]
    assert result["probe"] == {"container": "matroska"}
    assert "scan_job_id" not in result


async def test_diagnose_deep_starts_background_scan_job(monkeypatch):
    _patch_resolve(monkeypatch, {"item_type": "mv", "title": "Foo"})

    async def fake_probe(rating_key: str) -> Dict[str, Any]:
        return {"ok": True}

    started: List[str] = []

    async def fake_start_scan(
        rating_key: str, duration: Optional[float] = None
    ) -> Dict[str, Any]:
        started.append(rating_key)
        return {"job_id": "job-abc", "status": "running"}

    monkeypatch.setattr(mcp_server, "aprobe", fake_probe)
    monkeypatch.setattr(mcp_server, "astart_scan", fake_start_scan)

    result = await mcp_server.diagnose_video("123", deep=True)
    assert started == ["123"]
    assert result["scan_job_id"] == "job-abc"


async def test_diagnose_raises_for_unknown_rating_key(monkeypatch):
    _patch_resolve(monkeypatch, None)
    # @safe_tool converts the tool's ValueError into a ToolError so the message
    # reaches the model even with error masking on.
    with pytest.raises(ToolError):
        await mcp_server.diagnose_video("123")


# --------------------------------------------------------------------------
# repair_video
# --------------------------------------------------------------------------


async def test_repair_remux_starts_background_job(monkeypatch):
    _patch_resolve(monkeypatch, {"item_type": "mv", "title": "Foo"})
    started: List[str] = []

    async def fake_start_remux(rating_key: str) -> Dict[str, Any]:
        started.append(rating_key)
        return {"job_id": "job-xyz", "status": "running"}

    monkeypatch.setattr(mcp_server, "astart_remux", fake_start_remux)

    result = await mcp_server.repair_video("123", "remux")
    assert started == ["123"]
    assert result["mode"] == "remux"
    assert result["job_id"] == "job-xyz"


async def test_get_video_job_returns_state(monkeypatch):
    async def fake_get_job(job_id: str) -> Optional[Dict[str, Any]]:
        return {"id": job_id, "status": "running", "progress": 42.0}

    monkeypatch.setattr(mcp_server, "aget_job", fake_get_job)
    result = await mcp_server.get_video_job("job-abc")
    assert result["progress"] == 42.0


async def test_get_video_job_unknown_raises(monkeypatch):
    async def fake_get_job(job_id: str) -> Optional[Dict[str, Any]]:
        return None

    monkeypatch.setattr(mcp_server, "aget_job", fake_get_job)
    with pytest.raises(ToolError):
        await mcp_server.get_video_job("nope")


async def test_tool_annotations_split_read_write_destructive():
    read = await mcp_server.mcp.get_tool("list_open_candidates")
    assert read.annotations.readOnlyHint is True

    write = await mcp_server.mcp.get_tool("add_item")
    assert write.annotations.readOnlyHint is False
    assert write.annotations.destructiveHint is False

    destructive = await mcp_server.mcp.get_tool("repair_video")
    assert destructive.annotations.readOnlyHint is False
    assert destructive.annotations.destructiveHint is True


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
    with pytest.raises(ToolError):
        await mcp_server.repair_video("123", "transcode")


async def test_safe_tool_converts_vdiag_error_to_tool_error(monkeypatch):
    _patch_resolve(monkeypatch, {"item_type": "mv", "title": "Foo"})

    async def boom(rating_key: str) -> Dict[str, Any]:
        raise VdiagError("vdiag probe failed (404): item has no file")

    monkeypatch.setattr(mcp_server, "aprobe", boom)
    with pytest.raises(ToolError) as exc:
        await mcp_server.diagnose_video("123")
    assert "no file" in str(exc.value)


async def test_safe_tool_converts_helper_value_error_to_tool_error(monkeypatch):
    _patch_resolve(
        monkeypatch, {"item_type": "mv", "title": "Foo", "imdb_id": "tt0099"}
    )

    async def boom(imdb_id: str) -> Dict[str, Any]:
        raise ValueError("no Radarr movie for imdb tt0099")

    monkeypatch.setattr(mcp_server, "aredownload_by_imdb", boom)
    with pytest.raises(ToolError) as exc:
        await mcp_server.repair_video("123", "redownload")
    assert "no Radarr movie" in str(exc.value)


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


async def test_aupgrade_by_tvdb_unmatched_episode_raises_without_searching(monkeypatch):
    async def fake_get_series(tvdb_id: int) -> Dict[str, Any]:
        return {"id": 3}

    commands: List[Dict[str, Any]] = []

    async def fake_query(cmd: str, post: bool = False, **kwargs: Any) -> Any:
        if cmd == "episode":
            return [{"id": 11, "seasonNumber": 1, "episodeNumber": 2}]
        if cmd == "command":
            commands.append(kwargs)
        return None

    monkeypatch.setattr(sonarr_utils, "aget_series", fake_get_series)
    monkeypatch.setattr(sonarr_utils, "asn_query", fake_query)

    # S9E9 isn't in the list — must fail, not fall back to a whole-series search.
    with pytest.raises(ValueError):
        await sonarr_utils.aupgrade_by_tvdb("555", 5, season=9, episode=9)
    assert commands == []


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


# --------------------------------------------------------------------------
# Apple TV compatibility assessment
# --------------------------------------------------------------------------


def test_compat_h264_aac_mp4_direct_plays():
    probe = {
        "container": "mov,mp4,m4a,3gp,3g2,mj2",
        "video_streams": [{"codec": "h264", "width": 1920, "height": 1080}],
        "audio_streams": [{"codec": "aac", "channels": 2}],
    }
    result = assess_compatibility(probe)
    assert result["verdict"] == "direct_play"
    assert result["transcode_reasons"] == []


def test_compat_dts_audio_forces_transcode():
    probe = {
        "container": "matroska,webm",
        "video_streams": [{"codec": "hevc", "width": 1920, "height": 1080}],
        "audio_streams": [{"codec": "dca", "channels": 6}],
    }
    result = assess_compatibility(probe)
    assert result["verdict"] == "transcode"
    assert result["video"][0]["ok"] is True
    assert result["audio"][0]["ok"] is False
    assert any("dca" in r for r in result["transcode_reasons"])


def test_compat_vp9_video_forces_transcode():
    probe = {
        "container": "matroska,webm",
        "video_streams": [{"codec": "vp9", "width": 1920, "height": 1080}],
        "audio_streams": [{"codec": "aac", "channels": 2}],
    }
    result = assess_compatibility(probe)
    assert result["verdict"] == "transcode"
    assert result["video"][0]["ok"] is False


def test_compat_4k_h264_flagged():
    probe = {
        "container": "mov,mp4",
        "video_streams": [{"codec": "h264", "width": 3840, "height": 2160}],
        "audio_streams": [{"codec": "aac", "channels": 2}],
    }
    result = assess_compatibility(probe)
    assert result["verdict"] == "transcode"
    assert any("1080p" in r for r in result["transcode_reasons"])


def test_compat_hevc_main10_4k_direct_plays():
    probe = {
        "container": "matroska,webm",
        "video_streams": [
            {
                "codec": "hevc",
                "profile": "Main 10",
                "level_readable": 5.1,
                "bit_depth": 10,
                "width": 3840,
                "height": 2160,
            }
        ],
        "audio_streams": [{"codec": "eac3", "channels": 6}],
    }
    result = assess_compatibility(probe)
    assert result["verdict"] == "direct_play"
    assert result["video"][0]["level"] == 5.1
    assert result["video"][0]["bit_depth"] == 10


def test_compat_h264_high10_profile_flagged():
    probe = {
        "container": "mov,mp4",
        "video_streams": [
            {"codec": "h264", "profile": "High 10", "width": 1920, "height": 1080}
        ],
        "audio_streams": [{"codec": "aac", "channels": 2}],
    }
    result = assess_compatibility(probe)
    assert result["verdict"] == "transcode"
    assert "High 10" in result["video"][0]["reason"]


def test_compat_h264_level_above_42_flagged():
    probe = {
        "container": "mov,mp4",
        "video_streams": [
            {
                "codec": "h264",
                "profile": "High",
                "level_readable": 5.1,
                "width": 1920,
                "height": 1080,
            }
        ],
        "audio_streams": [{"codec": "aac", "channels": 2}],
    }
    result = assess_compatibility(probe)
    assert result["verdict"] == "transcode"
    assert any("level" in r for r in result["transcode_reasons"])


def test_compat_12bit_video_flagged():
    probe = {
        "container": "matroska",
        "video_streams": [
            {
                "codec": "hevc",
                "profile": "Main 12",
                "bit_depth": 12,
                "width": 1920,
                "height": 1080,
            }
        ],
        "audio_streams": [{"codec": "aac", "channels": 2}],
    }
    result = assess_compatibility(probe)
    assert result["verdict"] == "transcode"


def test_compat_unknown_client_raises():
    with pytest.raises(ValueError):
        assess_compatibility({"container": "mp4"}, client="toaster")
