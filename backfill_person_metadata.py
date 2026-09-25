"""Backfill cast / director / TMDB metadata onto catalog rows that lack it.

Rows ingested before the TMDB-hydration step have no `attributes->'cast'`,
`director`, `tmdb_id`, or `tmdb_title`. The cast-history subagent and the
taste-signal cast cross-reference both key off those fields, so a sparse
denominator silently degrades their signal. This script fills the gap.

Matching is by title, never by `uid`: `uid` in this catalog is a source-level
identifier that is neither unique nor stable (ingestion has retitled rows in
place). Each row is looked up with TMDB `/search/movie` or `/search/tv`
(cleaned title + year) and the hit is validated against the row's title
before anything is written; if no hit is trustworthy, the row is skipped and
reported — never written blind.

Writes `attributes->'tmdb_id'` (int), `attributes->'tmdb_title'` (clean
title, used for Plex lookups), `attributes->'cast'` (top-N names, plain
strings — matching the existing shape), and `attributes->'director'` (mv
only; TV has no director). Keys that already hold a value are left alone;
the rest are merged into the existing JSONB.

Auth: the TMDB key is a v4 token, presented as `Authorization: Bearer` (the
same scheme `indexer_utils/tmdb.py` uses).

Usage:
    python backfill_person_metadata.py [--item-type mv|tv] [--scope all|added]
                                       [--limit N] [--sleep S] [--dry-run]
"""

import argparse
import asyncio
import json
import os
import re
import urllib.parse
import urllib.request

from decouple import config
from sqlalchemy import text

TMDB = "https://api.themoviedb.org/3"
CAST_LIMIT = 10


def tmdb_json(path: str) -> dict:
    req = urllib.request.Request(
        f"{TMDB}{path}",
        headers={
            "accept": "application/json",
            "Authorization": f"Bearer {config('TMDB_API_KEY')}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def clean_title(raw: str) -> str:
    """Turn '6.Underground.2019.1080p...-KAMIKAZE' into '6 Underground 2019'."""
    t = raw.encode("utf-8", "ignore").decode()
    t = re.sub(r"^b['\"]", "", t)
    t = re.sub(r"\.\d{4}p\..*", "", t)
    t = re.sub(r"[\.\-_]+", " ", t)
    t = re.sub(
        r"\b(AC3[. ]?\d|D[DH]D?P?[. ]?\d|x264|x265|H264|H265|HEVC|AVC|BluRay|Blu-ray|BDRip|BRRip|WEB-?DL|WEB-?Rip|WEB|HDCAM|CAM|iNTERNAL|INTERNAL|PROPER|REPACK|MULTi|MULTI|DUAL|REMUX)\b.*",
        " ",
        t,
        flags=re.I,
    )
    t = re.sub(r"\b\d{4}\b", lambda m: m.group(0), t)
    return re.sub(r"\s+", " ", t).strip()


def year_of(raw: str) -> int | None:
    m = re.search(r"\b(19|20)\d{2}\b", raw)
    return int(m.group(0)) if m else None


def _tokens(s: str) -> set[str]:
    return {t.lower() for t in re.split(r"[\s'&.:()/-]+", s) if len(t) > 2}


def titles_related(row_title: str, tmdb_title: str) -> bool:
    a, b = _tokens(row_title), _tokens(tmdb_title)
    if not b:
        return False
    return len(a & b) / len(b) >= 0.6


def fetch_person_data(item_type: str, row_title: str) -> dict | None:
    """Return {tmdb_id, tmdb_title, cast, director} validated against row_title, or None."""
    raw_year = year_of(row_title)
    clean = clean_title(row_title)
    clean_q = re.sub(r"\s+\d{4}$", "", clean)

    search = f"/search/movie?query={urllib.parse.quote(clean_q)}&language=en-US"
    if raw_year:
        search += f"&primary_release_year={raw_year}"
    if item_type == "tv":
        search = f"/search/tv?query={urllib.parse.quote(clean_q)}&language=en-US"
        if raw_year:
            search += f"&first_air_date_year={raw_year}"
    res = tmdb_json(search)
    hits = res.get("results", [])
    exact = [
        h
        for h in hits
        if (h.get("title") or h.get("name") or "").lower() == clean_q.lower()
    ]
    hit = exact[0] if exact else (hits[0] if hits else None)
    if hit and not titles_related(row_title, hit.get("title") or hit.get("name") or ""):
        hit = None  # wrong movie — do not write it onto this row

    if not hit:
        return None

    mid = hit["id"]
    if item_type == "tv":
        c = tmdb_json(f"/tv/{mid}/credits?language=en-US")
        # dict.fromkeys dedupes while keeping billing order.
        names = dict.fromkeys(c2["name"] for c2 in c.get("cast", []))
        cast = list(names)[:CAST_LIMIT]
        return {
            "tmdb_id": mid,
            "tmdb_title": hit.get("name"),
            "cast": cast,
            "director": None,
        }

    c = tmdb_json(f"/movie/{mid}/credits?language=en-US")
    cast = [x["name"] for x in c.get("cast", [])[:CAST_LIMIT]]
    director = next(
        (x["name"] for x in c.get("crew", []) if x.get("job") == "Director"), None
    )
    return {
        "tmdb_id": mid,
        "tmdb_title": hit.get("title"),
        "cast": cast,
        "director": director,
    }


SELECT_SQL = """
    SELECT i.uid, i.title, i.attributes
    FROM indexer_utils_ignoreitem i
    WHERE i.item_type = :it
      AND (
        NOT (i.attributes::jsonb ? 'cast')
        OR (i.attributes::jsonb)->>'tmdb_title' IS NULL
      )
"""


async def load_rows(session, item_type: str, scope: str, limit: int | None):
    sql = SELECT_SQL
    if scope == "added":
        sql += " AND i.added"
    sql += " ORDER BY i.title LIMIT :lim"
    rows = (
        await session.execute(text(sql), {"it": item_type, "lim": limit or 10_000})
    ).fetchall()
    return [
        {
            "uid": r.uid,
            "title": r.title,
            "attributes": r.attributes
            if isinstance(r.attributes, dict)
            else json.loads(r.attributes),
        }
        for r in rows
    ]


UPDATE_SQL = """
    UPDATE indexer_utils_ignoreitem
    SET attributes = attributes::jsonb
            || (:patch)::jsonb
    WHERE uid = :uid AND title = :title
"""


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--item-type", choices=["mv", "tv"], default="mv")
    ap.add_argument("--scope", choices=["all", "added"], default="added")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--sleep", type=float, default=0.5, help="seconds between TMDB requests"
    )
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from indexer_utils.session import db_session

    async with db_session() as session:
        rows = await load_rows(session, args.item_type, args.scope, args.limit)
        print(f"{len(rows)} rows to consider")

        skipped, written, errors = 0, 0, 0
        for i, row in enumerate(rows, 1):
            if args.dry_run and i > 20:
                print(f"  ... {len(rows) - 20} more (dry run stops at 20)")
                break
            try:
                data = await asyncio.to_thread(
                    fetch_person_data, args.item_type, row["title"]
                )
                await asyncio.sleep(args.sleep)
            except Exception as e:
                errors += 1
                print(f"  [{i}] ERROR {row['title']!r}: {e}")
                continue
            if not data:
                skipped += 1
                print(f"  [{i}] SKIP (no trustworthy TMDB match): {row['title']!r}")
                continue

            if not (data["cast"] or data["director"]):
                skipped += 1
                print(f"  [{i}] SKIP (no cast/director returned): {row['title']!r}")
                continue

            # Only fill keys that are empty on the row; `||` would otherwise
            # overwrite values ingestion already wrote.
            existing = row["attributes"]
            patch: dict = {k: v for k, v in data.items() if v and not existing.get(k)}
            if not patch:
                skipped += 1
                print(f"  [{i}] SKIP (nothing missing): {row['title']!r}")
                continue
            print(
                f"  [{i}] {row['title']!r} -> {data['tmdb_title']!r} ({len(data['cast'])} cast)"
            )
            if not args.dry_run:
                r = await session.execute(
                    text(UPDATE_SQL),
                    {
                        "uid": row["uid"],
                        "title": row["title"],
                        "patch": json.dumps(patch),
                    },
                )
                assert r.rowcount >= 1, f"row vanished: {row['uid']} {row['title']!r}"
                written += 1
                if written % 25 == 0:
                    await session.commit()
                    print(f"  ...committed {written}")

        if not args.dry_run:
            await session.commit()
        print(
            f"done: {written} written, {skipped} skipped, {errors} errors (dry_run={args.dry_run})"
        )


if __name__ == "__main__":
    os.environ.setdefault(
        "PYDECOUPLE_CONFIG", os.path.join(os.path.dirname(__file__), ".env")
    )
    asyncio.run(main())
