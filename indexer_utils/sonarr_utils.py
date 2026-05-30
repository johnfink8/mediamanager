import asyncio
import json
from typing import Any, Dict, List, Optional

import requests
from decouple import config
from requests.auth import HTTPBasicAuth


def _sn_request(cmd: str, method: str, **kwargs: object) -> object:
    session = requests.Session()
    session.headers["X-Api-Key"] = config("SONARR_APIKEY")
    url = "/".join([config("SONARR_URL"), cmd])
    auth = None
    if config("SONARR_USERNAME", None):
        auth = HTTPBasicAuth(config("SONARR_USERNAME"), config("SONARR_PASSWORD"))
    if method in ("get", "delete"):
        r = getattr(session, method)(url, params=kwargs, auth=auth)
    else:
        r = getattr(session, method)(url, json=kwargs, auth=auth)
    if r.status_code >= 400:
        raise Exception(r.status_code, r.text)
    if not r.text:
        return None
    return json.loads(r.text)


def sn_query(cmd: str, post: bool = False, **kwargs: object) -> object:
    return _sn_request(cmd, "post" if post else "get", **kwargs)


SERIES: List[object] = []
_series_lock: Optional[asyncio.Lock] = None


def _get_series_lock() -> asyncio.Lock:
    global _series_lock
    if _series_lock is None:
        _series_lock = asyncio.Lock()
    return _series_lock


def reset_series() -> None:
    SERIES.clear()


def get_series(tvdb_id: int) -> Optional[Dict[str, object]]:
    tvdb_id = int(tvdb_id)
    if not SERIES:
        SERIES.extend(sn_query("series"))
    for series in SERIES:
        if series.get("tvdbId") == tvdb_id:
            return series
    return None


async def aget_series(tvdb_id: int) -> Optional[Dict[str, object]]:
    tvdb_id = int(tvdb_id)
    async with _get_series_lock():
        if not SERIES:
            fetched = await asyncio.to_thread(sn_query, "series")
            SERIES.extend(fetched)
    for series in SERIES:
        if series.get("tvdbId") == tvdb_id:
            return series
    return None


def query_series(tvdb: str) -> Dict[str, object]:
    return sn_query("series/lookup", term="tvdb:" + tvdb)[0]


async def aquery_series(tvdb: str) -> Dict[str, object]:
    return await asyncio.to_thread(query_series, tvdb)


async def asn_query(cmd: str, post: bool = False, **kwargs: Any) -> object:
    return await asyncio.to_thread(sn_query, cmd, post, **kwargs)


async def asn_delete(cmd: str, **kwargs: Any) -> object:
    return await asyncio.to_thread(_sn_request, cmd, "delete", **kwargs)


async def asn_put(cmd: str, **kwargs: Any) -> object:
    return await asyncio.to_thread(_sn_request, cmd, "put", **kwargs)


async def aregrab_episode(episode_id: int, replace_file: bool = True) -> Dict[str, Any]:
    """Re-grab a single episode, optionally deleting the existing file first.

    Deleting forces a replacement even when the current file already meets the
    quality cutoff; set ``replace_file=False`` to only search for an upgrade.
    """
    ep: Any = await asn_query(f"episode/{episode_id}")
    file_id = (ep or {}).get("episodeFileId") or 0
    deleted = False
    if replace_file and file_id:
        await asn_delete(f"episodefile/{file_id}")
        deleted = True
    await asn_query("command", post=True, name="EpisodeSearch", episodeIds=[episode_id])
    return {
        "episode_id": episode_id,
        "deleted_old_file": deleted,
        "status": "searching",
    }


async def aredownload_episode(
    tvdb_id: str, season: int, episode: int
) -> Dict[str, Any]:
    """Find a series' episode by season/number and re-grab a fresh copy."""
    series: Any = await aget_series(int(tvdb_id))
    if not series:
        raise ValueError(f"no Sonarr series for tvdb {tvdb_id}")
    eps: Any = await asn_query("episode", seriesId=series.get("id"))
    for candidate in eps or []:
        if (
            candidate.get("seasonNumber") == season
            and candidate.get("episodeNumber") == episode
        ):
            return await aregrab_episode(candidate["id"], replace_file=True)
    raise ValueError(f"no S{season}E{episode} for tvdb {tvdb_id}")


async def _episode_id_for(series_id: int, season: int, episode: int) -> Optional[int]:
    eps: Any = await asn_query("episode", seriesId=series_id)
    for candidate in eps or []:
        if (
            candidate.get("seasonNumber") == season
            and candidate.get("episodeNumber") == episode
        ):
            episode_id: int = candidate["id"]
            return episode_id
    return None


async def aupgrade_series(
    series_id: int,
    quality_profile_id: Optional[int] = None,
    episode_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Raise a series' quality profile (optional) and search for the upgrade.

    Quality in Sonarr lives on the series, not the episode, so an upgrade sets
    the series profile then searches — just the one episode when ``episode_id``
    is given, otherwise the whole series.
    """
    series: Any = await asn_query(f"series/{series_id}")
    if quality_profile_id is not None:
        series["qualityProfileId"] = quality_profile_id
        series["monitored"] = True
        await asn_put(f"series/{series_id}", **series)
    if episode_id is not None:
        await asn_query(
            "command", post=True, name="EpisodeSearch", episodeIds=[episode_id]
        )
    else:
        await asn_query("command", post=True, name="SeriesSearch", seriesId=series_id)
    return {
        "series_id": series_id,
        "quality_profile_id": series.get("qualityProfileId"),
        "episode_id": episode_id,
        "status": "searching",
    }


async def aupgrade_by_tvdb(
    tvdb_id: str,
    quality_profile_id: Optional[int] = None,
    season: Optional[int] = None,
    episode: Optional[int] = None,
) -> Dict[str, Any]:
    """Resolve a tvdb id to a Sonarr series and upgrade its quality.

    With ``season``+``episode`` only that episode is re-grabbed at the new
    quality; without them the whole series is searched.
    """
    series: Any = await aget_series(int(tvdb_id))
    if not series:
        raise ValueError(f"no Sonarr series for tvdb {tvdb_id}")
    series_id = series["id"]
    episode_id: Optional[int] = None
    if season is not None and episode is not None:
        episode_id = await _episode_id_for(series_id, season, episode)
    return await aupgrade_series(series_id, quality_profile_id, episode_id)


def add_series(tvdb: str, all_seasons: bool = True) -> None:
    series = query_series(tvdb)
    seasons = series["seasons"][:]
    if all_seasons:
        for season in seasons:
            season["monitored"] = True
    else:
        seasons[-1]["monitored"] = True
    series["addOptions"] = {
        "monitor": "all",
        "searchForMissingEpisodes": True,
        "searchForCutoffUnmetEpisodes": False,
    }
    series["rootFolderPath"] = "/store/TV"
    series["qualityProfileId"] = 5
    series.update(
        {
            "languageProfileId": 1,
            "seasonFolder": True,
            "monitored": True,
            "useSceneNumbering": False,
        }
    )
    series = sn_query("series", post=True, **series)
