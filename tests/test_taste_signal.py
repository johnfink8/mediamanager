"""build_taste_signal — the per-recommendation cohort cross-tab block.

Runs against the real pgvector postgres (see conftest). Redis is disabled so the
scored cohort is always computed live and never leaks across tests.
"""

import pytest
import pytest_asyncio

from indexer_utils import taste_signal as ts
from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session
from indexer_utils.taste_signal import build_taste_signal

VECTOR_DIMS = 1536
_VEC = [1.0] + [0.0] * (VECTOR_DIMS - 1)
CAND_ATTRS = {
    "genres": ["Horror"],
    "originalLanguage": ["English"],
    "rottenTomatoesuser_value": 90,
}


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Force live cohort computation; no cache bleed across truncated tests."""
    monkeypatch.setattr(ts, "get_redis_client", lambda: None)


def _item(uid, *, added, has_critic, genre, year=2025, ignore=True):
    attrs = {"year": year, "genres": [genre], "originalLanguage": ["English"]}
    if has_critic:
        attrs["rottenTomatoesuser_value"] = 80
    return IgnoreItem(
        uid=uid,
        title=uid,
        item_type="mv",
        added=added,
        ignore=ignore,
        shown=True,
        attributes=attrs,
        synopsis_vector=_VEC,
    )


@pytest_asyncio.fixture
async def session():
    async with db_session() as s:
        yield s


@pytest_asyncio.fixture
async def cohort(session):
    """8 decided 2025 movies (4 added; 5 with a critic rating), plus an
    undecided and an out-of-era title that must be excluded from the cohort."""
    rows = [
        _item("h-add-1", added=True, has_critic=True, genre="Horror"),
        _item("h-add-2", added=True, has_critic=True, genre="Horror"),
        _item("h-add-3", added=True, has_critic=True, genre="Horror"),
        _item("c-add-1", added=True, has_critic=False, genre="Comedy"),
        _item("h-no-1", added=False, has_critic=True, genre="Horror"),
        _item("h-no-2", added=False, has_critic=True, genre="Horror"),
        _item("c-no-1", added=False, has_critic=False, genre="Comedy"),
        _item("c-no-2", added=False, has_critic=False, genre="Comedy"),
        _item("undecided", added=False, has_critic=False, genre="Horror", ignore=False),
        _item("old", added=True, has_critic=True, genre="Horror", year=2010),
    ]
    for r in rows:
        session.add(r)
    await session.commit()
    return session


async def _build(session):
    return await build_taste_signal(
        session,
        item_type="mv",
        year=2025,
        candidate_attrs=CAND_ATTRS,
        candidate_vec=_VEC,
        candidate_uid="cand",
    )


async def test_block_has_expected_sections(cohort):
    block = await _build(cohort)

    assert set(block) == {
        "cohort",
        "candidate",
        "neighbor_x_critic",
        "by_attribute",
        "nearest",
    }


async def test_cohort_counts_decided_same_era_only(cohort):
    block = await _build(cohort)

    # 8 decided 2025 titles, 4 added — undecided and the 2010 title excluded.
    assert block["cohort"]["n"] == 8
    assert block["cohort"]["added"] == 4


async def test_matrix_partitions_the_cohort(cohort):
    block = await _build(cohort)

    cells = block["neighbor_x_critic"].values()
    assert sum(c["n"] for c in cells) == block["cohort"]["n"]
    assert sum(c["added"] for c in cells) == block["cohort"]["added"]


async def test_by_attribute_counts_candidate_values(cohort):
    block = await _build(cohort)

    # Horror cohort: 3 added of 5. English: 4 added of all 8.
    assert block["by_attribute"]["genre"]["horror"] == {"added": 3, "n": 5}
    assert block["by_attribute"]["language"]["english"] == {"added": 4, "n": 8}


async def test_candidate_indexed_by_critic_and_stratum(cohort):
    block = await _build(cohort)

    assert block["candidate"]["has_critic_rating"] is True
    # Candidate sees the 8 decided neighbours, 4 added: rate 0.5 == cohort 0.5,
    # which counts as below_base; with a critic rating → below_base + has_critic.
    assert block["candidate"]["of"] == 8
    assert block["candidate"]["neighbors_added"] == 4
    assert block["candidate"]["cell"] == "below_base + has_critic"


async def test_nearest_excludes_undecided_and_out_of_era(cohort):
    block = await _build(cohort)

    titles = {n["title"] for n in block["nearest"]}
    assert "undecided" not in titles
    assert "old" not in titles
