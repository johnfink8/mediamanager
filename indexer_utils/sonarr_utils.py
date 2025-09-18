import json
from typing import Dict, List, Optional

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


def query_series(tvdb: str) -> Dict[str, object]:
    return sn_query("series/lookup", term="tvdb:" + tvdb)[0]


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
