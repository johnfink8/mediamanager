from typing import Any, Dict, Optional
from urllib.parse import quote_plus

import requests
from decouple import config


def find_movie(title: str, year: Optional[int] = None) -> Optional[Dict[str, Any]]:
    url = config("PLEX_URL")
    headers = {
        "X-Plex-Token": config("PLEX_TOKEN"),
        "Accept": "application/json",
    }
    r = requests.get(
        url + "/search?query=%s" % quote_plus(title.encode(errors="ignore")),
        headers=headers,
    )
    results = r.json()
    if "Metadata" not in results["MediaContainer"]:
        return None
    for movie in results["MediaContainer"]["Metadata"]:
        if movie["title"].lower() == title.lower():
            if year is None or movie["year"] == year:
                return movie
    return None
