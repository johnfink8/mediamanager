"""Filtered similarity search through the HNSW index returns a full page.

pgvector's HNSW scan yields only ``hnsw.ef_search`` (40) nearest rows from
the whole table and applies WHERE filters afterwards, so a search scoped to
a small slice (added TV items) came back short or empty. ``session.py``
turns on iterative scan for every connection; this pins that, with the
index forced over a catalog where TV is one title in twenty.
"""

import random
from typing import Any, List

import pytest_asyncio
from agents import RunContextWrapper
from sqlalchemy import text

from indexer_utils.ai_tools import searches
from indexer_utils.ai_tools.base import ToolContext
from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session

DIMS = 768
INDEX = "test_synopsis_vector_hnsw"
_rnd = random.Random(7)


def _vec() -> List[float]:
    return [_rnd.gauss(0, 1) for _ in range(DIMS)]


QUERY = _vec()


@pytest_asyncio.fixture
async def library() -> Any:
    """600 added titles, one in twenty of them TV — like the real catalog."""
    async with db_session() as s:
        for i in range(600):
            item_type = "tv" if i % 20 == 0 else "mv"
            s.add(
                IgnoreItem(
                    uid=f"{item_type}-{i}",
                    title=f"{item_type}-{i}",
                    item_type=item_type,
                    added=True,
                    ignore=True,
                    shown=True,
                    attributes={"year": 2020},
                    synopsis_vector=_vec(),
                )
            )
        await s.commit()
        await s.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS {INDEX} ON indexer_utils_ignoreitem "
                "USING hnsw (synopsis_vector vector_cosine_ops)"
            )
        )
        await s.commit()
    yield
    async with db_session() as s:
        await s.execute(text(f"DROP INDEX IF EXISTS {INDEX}"))
        await s.commit()


async def test_connections_use_iterative_scan() -> None:
    async with db_session() as s:
        setting = (await s.execute(text("SHOW hnsw.iterative_scan"))).scalar()
    assert setting == "strict_order"


async def test_filtered_search_fills_the_page(library: Any, monkeypatch: Any) -> None:
    async def embed(_: str) -> List[float]:
        return QUERY

    monkeypatch.setattr("indexer_utils.vector_search._embed", embed)
    # Make the planner use the index, as it does on the real-sized table.
    monkeypatch.setattr(
        "indexer_utils.ai_tools.searches.db_session", _index_only_session
    )
    wrapper = RunContextWrapper(context=ToolContext(item_type="tv", candidate={}))
    out = await searches.search_similar_by_synopsis.__wrapped__(
        wrapper, query="q", limit=10
    )
    rows = out["results"]
    # Without iterative scan this was 1 row: the index's 40 nearest are
    # almost all movies, and the TV filter runs after.
    assert len(rows) == 10
    assert all(r["uid"].startswith("tv-") for r in rows)
    distances = [r["distance"] for r in rows]
    assert distances == sorted(distances)


class _index_only_session:
    async def __aenter__(self) -> Any:
        self._s = db_session()
        s = await self._s.__aenter__()
        await s.execute(text("SET enable_seqscan = off"))
        return s

    async def __aexit__(self, *exc: Any) -> None:
        await self._s.__aexit__(*exc)
