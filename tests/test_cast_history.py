"""search_cast_history — cast/director track-record subagent plumbing.

Runs against the real pgvector postgres (see conftest). The inner LLM call is
stubbed out: these tests cover the person-name resolution, the cross-reference
SQL (cast + director, leave-one-out, type scope, role flags), plex annotation
semantics, and the tool's no-data path. Live dossier quality is validated by
hand against known followed/avoided actors.
"""

import pytest
import pytest_asyncio
from agents import RunContextWrapper

from indexer_utils.ai_tools import cast_history as ch
from indexer_utils.ai_tools.base import ToolContext
from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session


def _raw(tool):
    """The undecorated function behind a @safe_tool FunctionTool."""
    return tool.__wrapped__ if hasattr(tool, "__wrapped__") else tool


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    monkeypatch.setattr(ch, "get_redis_client", lambda: None)


@pytest_asyncio.fixture
async def session():
    async with db_session() as s:
        yield s


def _item(uid, *, item_type, added, cast=None, director=None, year=2025):
    attrs = {"year": year}
    if cast:
        attrs["cast"] = cast
    if director:
        attrs["director"] = director
    return IgnoreItem(
        uid=uid,
        title=uid,
        item_type=item_type,
        added=added,
        ignore=True,
        shown=True,
        attributes=attrs,
    )


async def _seed_cross_ref(session):
    """Library rows for four people, around candidate 'cand-1'."""
    rows = [
        # Alpha Actor — cast only: 2 added, 1 not, 1 candidate (excluded),
        # 1 tv row (type-scoped out)
        _item("alpha-1", item_type="mv", added=True, cast=["Alpha Actor"]),
        _item("alpha-2", item_type="mv", added=True, cast=["Alpha Actor"]),
        _item("alpha-3", item_type="mv", added=False, cast=["Alpha Actor"]),
        _item("cand-1", item_type="mv", added=True, cast=["Alpha Actor"]),
        _item("alpha-tv", item_type="tv", added=True, cast=["Alpha Actor"]),
        # Beta Director — director field only
        _item("beta-1", item_type="mv", added=True, director="Beta Director"),
        _item("beta-2", item_type="mv", added=False, director="Beta Director"),
        # Gamma Both — same title in cast and as director
        _item(
            "gamma-1",
            item_type="mv",
            added=True,
            cast=["Gamma Both"],
            director="Gamma Both",
        ),
        # cand-1's own cast/director for the tool test
    ]
    session.add_all(rows)
    await session.commit()


class TestPersonNames:
    def test_cast_strings_and_dicts_deduped_in_order(self):
        cand = {"cast": ["A", {"name": "B"}, "", {"name": "A"}, "C"], "director": "D"}
        assert ch._person_names(cand) == ["A", "B", "C", "D"]

    def test_cast_capped_and_director_not_duplicated(self):
        cast = [f"Actor {i}" for i in range(12)]
        cand = {"cast": cast, "director": "Actor 3"}
        names = ch._person_names(cand)
        assert len(names) == 10
        assert names == cast[:10]

    def test_director_alone(self):
        assert ch._person_names({"director": "Solo"}) == ["Solo"]

    def test_empty(self):
        assert ch._person_names({}) == []
        assert ch._person_names({"cast": None, "director": None}) == []


class TestCrossRef:
    async def test_roles_counts_and_exclusions(self, session):
        await _seed_cross_ref(session)
        out = await ch._cross_ref(
            "mv",
            ["Alpha Actor", "Beta Director", "Gamma Both", "Nobody Known"],
            "cand-1",
        )

        alpha = out["alpha actor"]
        assert alpha["added"] == 2
        assert {t["t"] for t in alpha["titles"]} == {"alpha-1", "alpha-2", "alpha-3"}
        assert all(t["role"] == "cast" for t in alpha["titles"])
        # leave-one-out: the candidate itself is not in the cross-ref
        assert "cand-1" not in {t["t"] for t in alpha["titles"]}

        beta = out["beta director"]
        assert beta["added"] == 1
        assert all(t["role"] == "dir" for t in beta["titles"])

        gamma = out["gamma both"]
        assert gamma["added"] == 1
        assert gamma["titles"][0]["role"] == "both"

        # no library rows → absent key, not an empty block
        assert "nobody known" not in out

    async def test_type_scoping(self, session):
        await _seed_cross_ref(session)
        out = await ch._cross_ref("tv", ["Alpha Actor"], "cand-1")
        alpha = out["alpha actor"]
        assert {t["t"] for t in alpha["titles"]} == {"alpha-tv"}


class TestPlexAnnotate:
    async def test_status_semantics_and_cap(self, monkeypatch):
        calls = []

        async def fake_plex(title, year):
            calls.append(title)
            if title == "Boom":
                raise RuntimeError("plex down")
            return (
                {"viewCount": 1} if title in ("Present Added", "Present Not") else None
            )

        monkeypatch.setattr(ch, "aget_plex_details", fake_plex)
        titles = [
            {"t": "present-added.raw", "tt": "Present Added", "y": 2025, "added": True},
            {"t": "gone-added.raw", "tt": "Gone Added", "y": 2024, "added": True},
            {"t": "present-not.raw", "tt": "Present Not", "y": 2023, "added": False},
            {"t": "absent-not.raw", "tt": "Absent Not", "y": 2022, "added": False},
            {"t": "boom.raw", "tt": "Boom", "y": 2021, "added": True},
            # no clean title: a raw filename alone must never be labelled
            # "missing" — Plex's exact-title gate just can't match it
            {"t": "filename-only.2020.1080p.x264", "y": 2020, "added": True},
        ]
        await ch._plex_annotate("mv", titles)

        assert titles[0]["plex"] == "in_library"
        assert titles[1]["plex"] == "missing"
        assert titles[2]["plex"] == "in_library"
        assert "plex" not in titles[3]
        # infrastructure failure must not read as "deleted"
        assert "plex" not in titles[4]
        # a filename-only row gets no plex verdict at all
        assert "plex" not in titles[5]
        # ...and is never queried
        assert len(calls) == 5

    async def test_cap_limits_lookups(self, monkeypatch):
        calls = []

        async def fake_plex(title, year):
            calls.append(title)
            return None

        monkeypatch.setattr(ch, "aget_plex_details", fake_plex)
        titles = [
            {"t": f"t{i}", "tt": f"T {i}", "y": 2025 - (i // 10), "added": i < 10}
            for i in range(25)
        ]
        await ch._plex_annotate("mv", titles)
        assert len(calls) == ch.PLEX_LOOKUP_CAP

    async def test_tv_is_skipped(self, monkeypatch):
        async def fake_plex(title, year):
            raise AssertionError("plex must not be queried for tv")

        monkeypatch.setattr(ch, "aget_plex_details", fake_plex)
        titles = [{"t": "t", "y": 2025, "added": True}]
        await ch._plex_annotate("tv", titles)
        assert "plex" not in titles[0]


class TestTool:
    def _wrapper(self, candidate):
        return RunContextWrapper(
            context=ToolContext(item_type="mv", candidate=candidate)
        )

    async def test_no_cast_data_short_circuits(self, monkeypatch):
        async def boom(cache_key, user_prompt):
            raise AssertionError("subagent must not run without cast data")

        monkeypatch.setattr(ch, "_fetch_dossier", boom)
        out = await _raw(ch.search_cast_history)(self._wrapper({}))
        assert out == {"no_cast_data": True}

    async def test_dossier_path(self, monkeypatch, session):
        await _seed_cross_ref(session)
        seen = {}

        async def fake_dossier(cache_key, user_prompt):
            seen["key"] = cache_key
            seen["prompt"] = user_prompt
            return {"as_of": "2026-01-01", "report": "dossier"}

        monkeypatch.setattr(ch, "_fetch_dossier", fake_dossier)
        monkeypatch.setattr(ch, "aget_plex_details", lambda *a: _none())

        candidate = {
            "uid": "cand-1",
            "title": "cand-1",
            "year": 2025,
            "cast": ["Alpha Actor", "Nobody Known"],
            "director": "Beta Director",
        }
        out = await _raw(ch.search_cast_history)(self._wrapper(candidate))
        assert out["report"] == "dossier"
        assert (
            seen["key"] == f"mediamanager:cast_history:{ch.CACHE_KEY_VERSION}:mv:cand-1"
        )
        import json

        payload = json.loads(seen["prompt"])
        assert set(payload["people"]) == {"alpha actor", "beta director"}
        assert payload["people"]["alpha actor"]["catalog"] == 3


async def _none():
    return None
