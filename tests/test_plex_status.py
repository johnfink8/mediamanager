"""``get_item_details`` finds a movie in Plex by IMDb id, not by exact title.

Catalog titles are often release names ("The.Death.Of.Robin.hood.2026.
1080p…"), which an exact title match never found, so every such added movie
was reported ``missing_from_library`` — "likely deleted", which the agent
read as a strong negative.
"""

from typing import Any, Dict, List

import pytest
from agents import RunContextWrapper

from indexer_utils import plex_utils
from indexer_utils.ai_tools import inspections
from indexer_utils.ai_tools.base import ToolContext
from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session

UID = "tt32273171"
RELEASE = "The.Death.Of.Robin.hood.2026.1080p.HC.DCPRip.AAC5.1-NeoNoir"


def _hit(imdb_id: str) -> Dict[str, Any]:
    return {
        "title": "The Death of Robin Hood",
        "year": 2026,
        "guid": "plex://movie/abc",
        "Guid": [{"id": f"imdb://{imdb_id}"}, {"id": "tmdb://1284465"}],
        "viewCount": 2,
    }


@pytest.mark.parametrize(
    "name, title",
    [
        (RELEASE, "The Death Of Robin hood"),
        ("2001.A.Space.Odyssey.1968.1080p.BluRay", "2001 A Space Odyssey"),
        ("Heat", "Heat"),
        ("Mission: Impossible 2", "Mission: Impossible 2"),
    ],
)
def test_release_title(name: str, title: str) -> None:
    assert plex_utils.release_title(name) == title


def test_matches_on_imdb_id_not_title(monkeypatch: Any) -> None:
    queries: List[str] = []

    def search(query: str, hub_type: str) -> List[Dict[str, Any]]:
        queries.append(query)
        return [_hit("tt0000001"), _hit(UID)]

    monkeypatch.setattr(plex_utils, "_search_hub", search)
    found = plex_utils.get_plex_details(UID, [None, "Robin Hood", "Robin Hood"])
    assert found is not None and found["viewCount"] == 2
    assert queries == ["Robin Hood"]
    assert plex_utils.get_plex_details("tt9999999", ["Robin Hood"]) is None


async def _details(monkeypatch: Any, lookup: Any) -> Dict[str, Any]:
    async with db_session() as s:
        s.add(
            IgnoreItem(
                uid=UID,
                title=RELEASE,
                item_type="mv",
                added=True,
                ignore=True,
                shown=True,
                attributes={"year": 2026},
            )
        )
        await s.commit()
    monkeypatch.setattr(inspections, "aget_plex_details", lookup)
    wrapper = RunContextWrapper(context=ToolContext(item_type="mv", candidate={}))
    out: Dict[str, Any] = await inspections.get_item_details.__wrapped__(
        wrapper, uid=UID
    )
    return out


async def test_added_movie_under_a_release_name_is_in_library(
    monkeypatch: Any,
) -> None:
    seen: Dict[str, Any] = {}

    async def lookup(imdb_id: str, titles: List[str]) -> Dict[str, Any]:
        seen.update(imdb_id=imdb_id, titles=titles)
        return {"viewCount": 1}

    out = await _details(monkeypatch, lookup)
    assert out["plex_status"] == "in_library"
    assert seen == {"imdb_id": UID, "titles": [None, "The Death Of Robin hood"]}


async def test_absent_from_plex_is_missing(monkeypatch: Any) -> None:
    async def lookup(imdb_id: str, titles: List[str]) -> None:
        return None

    out = await _details(monkeypatch, lookup)
    assert out["plex_status"] == "missing_from_library"


async def test_plex_down_is_unknown_not_deleted(monkeypatch: Any) -> None:
    async def lookup(imdb_id: str, titles: List[str]) -> None:
        raise ConnectionError("plex unreachable")

    out = await _details(monkeypatch, lookup)
    assert out["plex_status"] == "unknown"
