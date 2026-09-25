"""backfill_person_metadata.pick_hit — choosing the TMDB result for a row."""

from backfill_person_metadata import _stored_year, pick_hit


def _movie(title, released, id_):
    return {"id": id_, "title": title, "release_date": released}


def _show(name, first_aired, id_):
    return {"id": id_, "name": name, "first_air_date": first_aired}


def test_movie_remake_is_resolved_by_year():
    hits = [_movie("Dune", "1984-12-14", 1), _movie("Dune", "2021-09-15", 2)]
    assert pick_hit("mv", hits, "Dune", 2021)["id"] == 2
    assert pick_hit("mv", hits, "Dune", 1984)["id"] == 1


def test_movie_with_no_hit_near_the_year_is_rejected():
    hits = [_movie("Dune", "1984-12-14", 1)]
    assert pick_hit("mv", hits, "Dune", 2021) is None


def test_series_picks_closest_premiere_not_after_the_row():
    hits = [
        _show("Shameless", "2011-01-09", 1),  # US
        _show("Shameless", "2004-01-13", 2),  # UK
        _show("Shameless", "2030-01-01", 3),  # premiered after the row: never
    ]
    # a later-season row: its year is past the premiere, not equal to it
    assert pick_hit("tv", hits, "Shameless", 2015)["id"] == 1
    assert pick_hit("tv", hits, "Shameless", 2007)["id"] == 2


def test_exact_title_beats_a_fuzzy_match():
    hits = [_movie("Heat Wave", "1995-01-01", 1), _movie("Heat", "1995-12-15", 2)]
    assert pick_hit("mv", hits, "Heat", 1995)["id"] == 2


def test_without_a_year_the_first_exact_match_wins():
    hits = [_show("Dirt", "2007-01-02", 1), _show("Dirt", "2019-01-01", 2)]
    assert pick_hit("tv", hits, "Dirt", None)["id"] == 1


def test_stored_year_parsing():
    assert _stored_year({"year": "2025"}) == 2025
    assert _stored_year({"year": 2019}) == 2019
    assert _stored_year({}) is None
    assert _stored_year({"year": "n/a"}) is None
