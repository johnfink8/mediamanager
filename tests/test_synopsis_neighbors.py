"""synopsis_neighbor_summary — the per-recommendation taste signal.

Runs against the real pgvector postgres (see conftest). Pins the
pool-restriction rules the recommendation read leans on: only *decided*
(``ignore=True``) items count, only same-era (``candidate_year ± window``)
items count, and ``base_rate`` is the keep rate of that whole pool — not just
the top-``k`` that get surfaced. A still-in-queue or wrong-era neighbour
silently counting as a negative is exactly what makes the agent over-veto
current releases.
"""

import pytest_asyncio

from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session
from indexer_utils.vector_search import synopsis_neighbor_summary

VECTOR_DIMS = 1536

# A dim-0 unit vector and two neighbours relative to it: aligned (cosine
# distance 0) and off-axis (~0.29), so "nearest" ordering is deterministic.
_CANDIDATE = [1.0] + [0.0] * (VECTOR_DIMS - 1)
_ALIGNED = [1.0] + [0.0] * (VECTOR_DIMS - 1)
_OFF_AXIS = [1.0, 1.0] + [0.0] * (VECTOR_DIMS - 2)


def _item(uid, *, added, ignore, year, vec=_ALIGNED):
    return IgnoreItem(
        uid=uid,
        title=uid,
        item_type="mv",
        added=added,
        ignore=ignore,
        shown=True,
        attributes={"year": year},
        synopsis_vector=vec,
    )


def _titles(result):
    return {n["title"] for n in result["nearest"]}


@pytest_asyncio.fixture
async def session():
    async with db_session() as s:
        yield s


@pytest_asyncio.fixture
async def pool(session):
    """4 decided in-era neighbours (3 kept, 1 passed) around year 2025, plus
    two the summary must exclude: an undecided in-era item and a kept item
    from a different era."""
    rows = [
        _item("kept-1", added=True, ignore=True, year=2025),
        _item("kept-2", added=True, ignore=True, year=2025),
        _item("kept-3", added=True, ignore=True, year=2024),
        _item("passed-1", added=False, ignore=True, year=2026, vec=_OFF_AXIS),
        _item("undecided", added=False, ignore=False, year=2025),
        _item("old-kept", added=True, ignore=True, year=2010),
    ]
    for r in rows:
        session.add(r)
    await session.commit()
    return session


async def test_undecided_items_are_not_neighbors(pool):
    """An ``ignore=False`` item is still in the review queue — neither kept nor
    passed — so it must not appear in the pool even when it's the closest."""
    result = await synopsis_neighbor_summary(pool, "mv", "cand", _CANDIDATE, 2025)

    assert "undecided" not in _titles(result)


async def test_out_of_era_items_are_not_neighbors(pool):
    result = await synopsis_neighbor_summary(pool, "mv", "cand", _CANDIDATE, 2025)

    assert result["era"] == [2023, 2027]
    assert "old-kept" not in _titles(result)


async def test_counts_kept_among_decided_same_era_neighbors(pool):
    result = await synopsis_neighbor_summary(pool, "mv", "cand", _CANDIDATE, 2025)

    assert result["k"] == 4  # undecided + out-of-era dropped
    assert result["added_of_top"] == 3


async def test_base_rate_spans_full_pool_not_just_top_k(pool):
    """base_rate is the era keep rate (3 kept of 4 decided = 0.75) even when
    ``k`` surfaces only the two nearest — so it can't collapse to
    ``added_of_top / k``."""
    result = await synopsis_neighbor_summary(pool, "mv", "cand", _CANDIDATE, 2025, k=2)

    assert result["k"] == 2
    assert result["base_rate"] == 0.75


async def test_missing_year_falls_back_to_all_eras(pool):
    result = await synopsis_neighbor_summary(pool, "mv", "cand", _CANDIDATE, None)

    assert result["era"] is None
    assert "old-kept" in _titles(result)


async def test_returns_none_without_decided_neighbors(session):
    session.add(_item("undecided", added=False, ignore=False, year=2025))
    await session.commit()

    result = await synopsis_neighbor_summary(session, "mv", "cand", _CANDIDATE, 2025)

    assert result is None
