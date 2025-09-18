import json
from typing import Any, Dict, List, Optional, cast

import requests
from decouple import config
from requests.auth import HTTPBasicAuth


def radarr_query(cmd: str, method: str = "get", **kwargs) -> List[Dict[str, Any]]:
    session = requests.Session()
    session.headers["X-Api-Key"] = config("RADARR_APIKEY")
    url = "/".join([config("RADARR_URL"), cmd])
    auth = None
    if config("RADARR_USERNAME", None):
        auth = HTTPBasicAuth(config("RADARR_USERNAME"), config("RADARR_PASSWORD"))
    if method == "get":
        resp = session.get(
            url,
            params=kwargs,
            auth=auth,
        )
    else:
        method_func = getattr(session, method)
        resp = method_func(
            url,
            json=kwargs,
            auth=auth,
        )
    txt = resp.text
    return json.loads(txt)


MOVIES: List[Dict[str, object]] = []


def reset_movies() -> None:
    del MOVIES[:]


def get_movie(imdbId: str) -> Optional[Dict[str, object]]:
    if not MOVIES:
        MOVIES.extend(cast(List[Dict[str, object]], radarr_query("movie")))

    for movie in MOVIES:
        if "imdbId" not in movie:
            continue
        if movie["imdbId"] == imdbId:
            return movie
    return None
