"""search_cast_history — cast/director track-record subagent plumbing.

Runs against the real pgvector postgres (see conftest). The inner LLM call
and the Plex search API are stubbed: these tests cover person-name
resolution, the cross-reference SQL (cast + director, leave-one-out, type
scope, role flags), the Plex presence-check semantics (verification gate,
absence vs infrastructure error, cap, tv skip), the check_titles tool
mapping, the web_search alias, and the tool plumbing. Live dossier
quality is validated by hand against known followed/avoided actors.
"""

import json
from datetime import date

import pytest
import pytest_asyncio
from agents import RunContextWrapper
from agents.tool_context import ToolContext as SdkToolContext

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


@pytest.fixture(autouse=True)
def _no_tmdb(monkeypatch):
    """TMDB never resolves anyone unless a test says otherwise."""
    monkeypatch.setattr(ch, "get_credit_person_ids", lambda item_type, tmdb_id: {})
    monkeypatch.setattr(ch, "search_person_id", lambda name: None)

    def no_credits(person_id):
        raise AssertionError("no person should resolve")

    monkeypatch.setattr(ch, "get_person_combined_credits", no_credits)


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


class TestPlexPresence:
    def _stub_hubs(self, monkeypatch, hubs):
        """hubs: hub_type → entry list, or the Exception to raise."""

        def fake_hub(query, hub_type):
            h = hubs[hub_type]
            if isinstance(h, Exception):
                raise h
            return h

        monkeypatch.setattr(ch, "hub_search", fake_hub)

    def test_present_in_movie_hub(self, monkeypatch):
        self._stub_hubs(
            monkeypatch,
            {"movie": [{"title": "Heat", "year": 1995}], "show": []},
        )
        res = ch._plex_presence("Heat", 1995)
        assert res == {"present": True, "plex_title": "Heat", "plex_year": 1995}

    def test_present_in_show_hub(self, monkeypatch):
        self._stub_hubs(
            monkeypatch,
            {"movie": [], "show": [{"title": "Silo", "year": 2023}]},
        )
        res = ch._plex_presence("Silo", 2023)
        assert res["present"] is True
        assert res["plex_title"] == "Silo"

    def test_year_tolerance_is_plus_minus_one(self, monkeypatch):
        self._stub_hubs(
            monkeypatch,
            {"movie": [{"title": "The Accountant", "year": 2016}], "show": []},
        )
        assert ch._plex_presence("the accountant", 2015)["present"] is True
        assert ch._plex_presence("the accountant", 2014)["present"] is False

    def test_same_name_other_work_rejected(self, monkeypatch):
        self._stub_hubs(
            monkeypatch,
            {
                "movie": [
                    {"title": "Deadpool 2", "year": 2018},
                    {"title": "Deadpool", "year": 2016},
                ],
                "show": [],
            },
        )
        res = ch._plex_presence("Deadpool", 2016)
        assert res == {"present": True, "plex_title": "Deadpool", "plex_year": 2016}

    def test_work_year_unknown_accepts_any(self, monkeypatch):
        self._stub_hubs(
            monkeypatch,
            {"movie": [{"title": "Memento", "year": 2000}], "show": []},
        )
        assert ch._plex_presence("Memento", None)["present"] is True

    def test_hub_failure_is_no_signal_not_absence(self, monkeypatch):
        self._stub_hubs(
            monkeypatch,
            {"movie": RuntimeError("plex down"), "show": []},
        )
        res = ch._plex_presence("Heat", 1995)
        assert "error" in res
        assert "present" not in res

    def test_empty_query_is_absent_without_calling_plex(self, monkeypatch):
        called = []

        def fake_hub(query, hub_type):
            called.append(hub_type)
            return []

        monkeypatch.setattr(ch, "hub_search", fake_hub)
        assert ch._plex_presence("   ", None) == {"present": False}
        assert called == []


class TestPlexAnnotate:
    async def test_status_semantics_and_cap(self, monkeypatch):
        calls = []

        def fake_presence(work, year):
            calls.append((work, year))
            if work == "Boom":
                return {"error": "plex movie search failed: RuntimeError"}
            return {"present": work in ("Present Added", "Present Not")}

        monkeypatch.setattr(ch, "_plex_presence", fake_presence)
        titles = [
            {"t": "present-added.raw", "tt": "Present Added", "y": 2025, "added": True},
            {"t": "gone-added.raw", "tt": "Gone Added", "y": 2024, "added": True},
            {"t": "present-not.raw", "tt": "Present Not", "y": 2023, "added": False},
            {"t": "absent-not.raw", "tt": "Absent Not", "y": 2022, "added": False},
            {"t": "boom.raw", "tt": "Boom", "y": 2021, "added": True},
            # no clean title: a raw release filename alone must never be
            # labelled "missing" — there is nothing clean to ask Plex for
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

        def fake_presence(work, year):
            calls.append(work)
            return {"present": False}

        monkeypatch.setattr(ch, "_plex_presence", fake_presence)
        titles = [
            {"t": f"t{i}", "tt": f"T {i}", "y": 2025 - (i // 10), "added": i < 10}
            for i in range(25)
        ]
        await ch._plex_annotate("mv", titles)
        assert len(calls) == ch.PLEX_LOOKUP_CAP

    async def test_tv_is_skipped(self, monkeypatch):
        def fake_presence(work, year):
            raise AssertionError("plex must not be queried for tv")

        monkeypatch.setattr(ch, "_plex_presence", fake_presence)
        titles = [{"t": "t", "y": 2025, "added": True}]
        await ch._plex_annotate("tv", titles)
        assert "plex" not in titles[0]


class TestCheckTitles:
    def _wrapper(self, item_type="mv", candidate=None):
        return RunContextWrapper(
            context=ToolContext(item_type=item_type, candidate=candidate or {})
        )

    async def test_present_absent_error_mapping(self, monkeypatch):
        def fake_presence(work, year):
            if work == "A":
                return {
                    "present": True,
                    "plex_title": "A",
                    "plex_year": 2016,
                }
            if work == "C":
                return {"error": "plex movie search failed: timeout"}
            return {"present": False}

        monkeypatch.setattr(ch, "_plex_presence", fake_presence)
        out = await _raw(ch.check_titles)(
            self._wrapper(),
            [
                ch.WorkQuery(title="A", year=2016),
                ch.WorkQuery(title="B", year=2010),
                ch.WorkQuery(title="C", year=None),
            ],
        )
        assert out["results"][0] == {
            "title": "A",
            "year": 2016,
            "present": True,
            "plex_title": "A",
            "plex_year": 2016,
        }
        assert out["results"][1] == {"title": "B", "year": 2010, "present": False}
        assert "error" in out["results"][2]
        assert "present" not in out["results"][2]

    async def test_cap_and_blank_titles_skipped(self, monkeypatch):
        calls = []

        def fake_presence(work, year):
            calls.append(work)
            return {"present": False}

        monkeypatch.setattr(ch, "_plex_presence", fake_presence)
        works = [ch.WorkQuery(title=f"W{i}", year=2020) for i in range(45)]
        works.append(ch.WorkQuery(title="   ", year=None))
        out = await _raw(ch.check_titles)(self._wrapper(), works)
        assert len(calls) == ch.TITLES_PER_CHECK_CAP
        assert len(out["results"]) == ch.TITLES_PER_CHECK_CAP
        assert out["item_type"] == "mv"


class TestWebSearchAlias:
    def test_tool_is_exposed_as_web_search(self):
        assert ch.web_search.name == "web_search"

    async def test_delegates_to_brave_search(self, monkeypatch):
        async def fake_brave(wrapper, query, count=10, freshness=""):
            return {"query": query, "count": count, "freshness": freshness}

        class FakeTool:
            __wrapped__ = fake_brave

        monkeypatch.setattr(ch, "brave_search", FakeTool)
        # Invoke through the SDK, not the raw function: a sync alias that
        # returns the un-awaited coroutine only shows up on this path.
        args = json.dumps({"query": "q", "count": 3})
        out = await ch.web_search.on_invoke_tool(
            SdkToolContext(
                context=ToolContext(item_type="mv", candidate={"uid": "x"}),
                tool_name="web_search",
                tool_call_id="call-1",
                tool_arguments=args,
            ),
            args,
        )
        assert out == {"query": "q", "count": 3, "freshness": ""}


class TestTool:
    def _wrapper(self, candidate):
        return RunContextWrapper(
            context=ToolContext(item_type="mv", candidate=candidate)
        )

    async def test_no_cast_data_short_circuits(self, monkeypatch):
        async def boom(cache_key, user_prompt, ctx):
            raise AssertionError("subagent must not run without cast data")

        monkeypatch.setattr(ch, "_fetch_dossier", boom)
        out = await _raw(ch.search_cast_history)(self._wrapper({}))
        assert out == {"no_cast_data": True}

    async def test_dossier_path_includes_seedless_people(self, monkeypatch, session):
        await _seed_cross_ref(session)
        seen = {}

        async def fake_dossier(cache_key, user_prompt, ctx):
            seen["key"] = cache_key
            seen["prompt"] = user_prompt
            seen["ctx"] = ctx
            return {"as_of": "2026-01-01", "report": "dossier"}

        def fake_presence(work, year):
            return {"present": False}

        monkeypatch.setattr(ch, "_fetch_dossier", fake_dossier)
        monkeypatch.setattr(ch, "_plex_presence", fake_presence)

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
        assert seen["ctx"] is not None

        payload = json.loads(seen["prompt"])
        # every person in the people-set is present, seed or not
        assert set(payload["people"]) == {
            "alpha actor",
            "beta director",
            "nobody known",
        }
        assert payload["people"]["alpha actor"]["catalog"] == 3
        assert payload["people"]["nobody known"]["catalog"] == 0
        # unresolvable in TMDB (see _no_tmdb): null, never an empty list
        assert payload["people"]["alpha actor"]["filmography"] is None
        assert "seed_note" in payload

    async def test_filmography_reaches_the_payload(self, monkeypatch, session):
        seen = {}

        async def fake_dossier(cache_key, user_prompt, ctx):
            seen["prompt"] = user_prompt
            return {"report": "dossier"}

        monkeypatch.setattr(ch, "_fetch_dossier", fake_dossier)
        monkeypatch.setattr(ch, "search_person_id", lambda name: 7)
        monkeypatch.setattr(
            ch,
            "get_person_combined_credits",
            lambda pid: {"cast": [_credit("Heat", "1995-12-15", order=0)]},
        )
        candidate = {"uid": "c", "cast": ["Solo Actor"], "director": None}
        await _raw(ch.search_cast_history)(self._wrapper(candidate))
        film = json.loads(seen["prompt"])["people"]["solo actor"]["filmography"]
        assert film == {
            "works": [{"t": "Heat", "y": 1995, "type": "movie"}],
            "total": 1,
        }

    async def test_cache_hit_skips_all_lookups(self, monkeypatch):
        cached = {"as_of": "2026-01-01", "report": "from cache"}
        monkeypatch.setattr(ch, "get_redis_client", lambda: object())
        monkeypatch.setattr(ch, "redis_get_json", lambda redis, key: cached)

        async def boom(*a, **k):
            raise AssertionError("a cache hit must not touch the DB, TMDB or the model")

        monkeypatch.setattr(ch, "_cross_ref", boom)
        monkeypatch.setattr(ch, "_filmographies", boom)
        monkeypatch.setattr(ch, "_fetch_dossier", boom)
        out = await _raw(ch.search_cast_history)(
            self._wrapper({"uid": "c", "cast": ["A"]})
        )
        assert out == cached


class TestFetchDossier:
    async def test_empty_output_is_an_error_not_a_crash(self, monkeypatch):
        class EmptyResult:
            final_output = ""
            raw_responses = [object()]

        class FakeRunner:
            @staticmethod
            async def run(*args, **kwargs):
                return EmptyResult()

        monkeypatch.setattr(ch, "Runner", FakeRunner)
        out = await ch._fetch_dossier(
            "k", "{}", ToolContext(item_type="mv", candidate={})
        )
        assert out["error"].startswith("subagent returned no dossier")


def _credit(title, released, *, media="movie", **extra):
    c = {
        "media_type": media,
        "id": hash((media, title)) & 0xFFFF,
        "vote_count": extra.pop("votes", 100),
        "genre_ids": extra.pop("genres", [18]),
        "character": extra.pop("character", "Lead"),
    }
    c["title" if media == "movie" else "name"] = title
    c["release_date" if media == "movie" else "first_air_date"] = released
    c.update(extra)
    return c


class TestFilmography:
    TODAY = date(2026, 9, 24)

    def _works(self, credits, *, acting=True, directing=False):
        return ch._filmography(
            credits, acting=acting, directing=directing, today=self.TODAY
        )

    def test_real_roles_only(self):
        credits = {
            "cast": [
                _credit("Lead Film", "2010-01-01", order=0),
                _credit("Bit Part", "2011-01-01", order=ch.MAX_BILLING_ORDER),
                _credit("Cameo", "2012-01-01", order=1, character="Anna (uncredited)"),
                _credit("Doc", "2013-01-01", order=0, character="Self"),
                _credit("Series Regular", "2014-01-01", media="tv", episode_count=10),
                _credit("Guest Spot", "2015-01-01", media="tv", episode_count=1),
                _credit(
                    "Talk Show",
                    "2016-01-01",
                    media="tv",
                    episode_count=50,
                    genres=[10767],
                ),
                _credit("Not Out Yet", "2027-01-01", order=0),
                _credit("No Date", "", order=0),
            ]
        }
        out = self._works(credits)
        assert [w["t"] for w in out["works"]] == ["Series Regular", "Lead Film"]
        assert out["total"] == 2

    def test_directing_uses_director_crew_only(self):
        credits = {
            "cast": [_credit("Acted In", "2010-01-01", order=0)],
            "crew": [
                _credit("Directed", "2018-01-01", job="Director"),
                _credit("Produced", "2019-01-01", job="Producer"),
            ],
        }
        directed = self._works(credits, acting=False, directing=True)
        assert [w["t"] for w in directed["works"]] == ["Directed"]
        both = self._works(credits, acting=True, directing=True)
        assert [w["t"] for w in both["works"]] == ["Directed", "Acted In"]

    def test_most_prominent_capped_then_newest_first(self):
        credits = {
            "cast": [
                _credit(f"W{i}", f"{1990 + i}-01-01", order=0, votes=i)
                for i in range(ch.FILMOGRAPHY_CAP + 5)
            ]
        }
        out = self._works(credits)
        years = [w["y"] for w in out["works"]]
        assert len(years) == ch.FILMOGRAPHY_CAP
        assert out["total"] == ch.FILMOGRAPHY_CAP + 5
        assert min(years) == 1995  # the 5 least-voted (oldest) dropped
        assert years == sorted(years, reverse=True)

    def test_candidate_itself_is_excluded(self):
        own = _credit("Candidate", "2026-01-01", order=0, id=42)
        other = _credit("Earlier", "2020-01-01", order=0, id=7)
        same_id_other_media = _credit(
            "Show", "2019-01-01", media="tv", episode_count=5, id=42
        )
        out = ch._filmography(
            {"cast": [own, other, same_id_other_media]},
            acting=True,
            directing=False,
            today=self.TODAY,
            exclude=("movie", 42),
        )
        assert [w["t"] for w in out["works"]] == ["Earlier", "Show"]

    def test_same_work_listed_twice_is_deduped(self):
        c = _credit("Heat", "1995-12-15", order=0)
        out = self._works({"cast": [c, dict(c)]})
        assert out["total"] == 1


class TestFilmographies:
    async def test_credits_ids_first_then_name_search(self, monkeypatch):
        searched = []
        monkeypatch.setattr(
            ch, "get_credit_person_ids", lambda it, tid: {"credited actor": 1}
        )

        def fake_search(name):
            searched.append(name)
            return 2

        monkeypatch.setattr(ch, "search_person_id", fake_search)
        fetched = []

        def fake_credits(pid):
            fetched.append(pid)
            return {"cast": [_credit(f"P{pid}", "2000-01-01", order=0)]}

        monkeypatch.setattr(ch, "get_person_combined_credits", fake_credits)
        out = await ch._filmographies(
            "mv",
            {"tmdb_id": 99, "cast": ["Credited Actor", "Other Actor"]},
            ["Credited Actor", "Other Actor"],
        )
        assert searched == ["Other Actor"]
        assert sorted(fetched) == [1, 2]
        assert out["credited actor"]["works"][0]["t"] == "P1"

    async def test_lookup_failure_is_none(self, monkeypatch):
        def boom(name):
            raise RuntimeError("tmdb down")

        monkeypatch.setattr(ch, "search_person_id", boom)
        out = await ch._filmographies("mv", {"cast": ["A"]}, ["A"])
        assert out == {"a": None}
