import asyncio
import json
from typing import Any, Dict, List, Optional

import requests
from decouple import config
from requests.auth import HTTPBasicAuth


def sn_query(cmd: str, post: bool = False, **kwargs: object) -> object:
    session = requests.Session()
    session.headers["X-Api-Key"] = config("SONARR_APIKEY")
    url = "/".join([config("SONARR_URL"), cmd])
    auth = None
    if config("SONARR_USERNAME", None):
        auth = HTTPBasicAuth(config("SONARR_USERNAME"), config("SONARR_PASSWORD"))
    if post:
        r = session.post(url, json=kwargs, auth=auth)
    else:
        r = session.get(url, params=kwargs, auth=auth)
    if r.status_code >= 400:
        raise Exception(r.status_code, r.text)
    return json.loads(r.text)


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
