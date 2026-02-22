import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from time import sleep
from typing import Any, Dict, List, Optional
from urllib import parse as urlparse
from urllib.parse import urlencode

import requests
import tvdb_v4_official
from decouple import config
from imdb import Cinemagoer

from .ai_recs import annotate_with_ai
from .check_feedback import record_check_result
from .filters import should_ignore_by_rules
from .models import IgnoreItem
from .plex_utils import find_movie
from .radarr_utils import get_movie, radarr_query, reset_movies
from .sonarr_utils import query_series, reset_series
from .tmdb import get_tv_cast, get_tv_id

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())

TVDB_API_KEY = config("TVDB_API_KEY", default=None)


def get_attr(item: ET.Element, name: str) -> Optional[str]:
    for child in item:
        if "name" in child.attrib and child.attrib["name"] == name:
            return child.attrib["value"]
    return None


def get_attrs(item: ET.Element) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for child in item:
        if "name" in child.attrib and "value" in child.attrib:
            attrs[child.attrib["name"]] = child.attrib["value"]
    return attrs


def get_ratings_attrs(ratings):
    attrs: Dict[str, str] = {}
    for rating_source in ratings:
        attrs[f"{rating_source}{ratings[rating_source]['type']}_votes"] = ratings[
            rating_source
        ]["votes"]
        attrs[f"{rating_source}{ratings[rating_source]['type']}_value"] = ratings[
            rating_source
        ]["value"]
    return attrs


def clean_radarr() -> None:
    for movie in radarr_query("movie"):
        if movie["downloaded"] and movie["monitored"]:
            movie["monitored"] = False
            radarr_query("movie", method="put", **movie)


def check_movies(days: int) -> None:
    started_at = datetime.utcnow()
    checked_movies = []
    success = False
    summary = "Movie check did not complete"
    error_details = None
    context = {
        "r": config("INDEXER_APIKEY"),
        "num": config("INDEXER_NUM"),
        "days": days,
        "t": "2040",
        "dl": "1",
        "i": "84818",
    }
    logger.info("Requesting from %s", config("INDEXER_URL"))
    for i in range(15):
        data = None
        try:
            data = requests.get(config("INDEXER_URL"), params=context).text  # type: ignore
            root = ET.fromstring(data)
            break
        except Exception:
            logger.exception("Error in requst")
            logger.error("Data: %s", data)
            sleep(0.2)
    else:
        raise Exception("SSL attempts exhausted")
    movies = []
    checked_movie_ids = set()
    already = []

    def already_have(imdb_id: str) -> bool:
        for movie in movies:
            if movie["imdb_id"] == imdb_id:
                return True
        return False

    def already_searched(imdb_id: str) -> bool:
        return imdb_id in already

    reset_movies()
    seen = set()
    try:
        for item in root.iter("item"):
            title = item.find("title").text  # type: ignore
            title_bytes = title.encode("ascii", "ignore")  # type: ignore
            normalized_title = title_bytes.decode("ascii", "ignore")
            attrs = get_attrs(item)
            imdb = get_attr(item, "imdb")
            if not imdb:
                logger.info("IMDB not found for %s", title)
                continue
            if imdb in seen:
                # we get a ton of duplicates now
                continue
            seen.add(imdb)
            imdb_id = "tt%07i" % int(imdb)

            def add_checked_item(note: str, *, ignored: Optional[bool] = None) -> None:
                if imdb_id in checked_movie_ids:
                    return
                checked_movie_ids.add(imdb_id)
                checked_movies.append(
                    {
                        "title": normalized_title,
                        "uid": imdb_id,
                        "ignored": ignored,
                        "note": note,
                    }
                )

            if IgnoreItem.exists("mv", imdb_id):
                add_checked_item("Already recorded in ignore list")
                continue
            if already_have(imdb_id):
                logger.info("Ignore movie already have %s", title)
                add_checked_item("Duplicate in current feed")
                continue
            try:
                radarr_movie = get_movie(imdb_id)
                if radarr_movie:
                    logger.info("Ignore movie in radarr %s", title)
                    add_checked_item("Already present in Radarr")
                    continue
            except KeyError:
                pass
            if already_searched(imdb_id):
                add_checked_item("Already queried during this run")
                continue
            already.append(imdb_id)
            try:
                result = radarr_query("movie/lookup", term="imdb:" + imdb_id)[0]
            except ValueError:
                logger.exception("Unable to search %s", title)
                add_checked_item("Radarr lookup failed", ignored=None)
                continue
            except IndexError:
                logger.exception("Unable to search %s", title)
                add_checked_item("No search results from Radarr", ignored=None)
                continue
            except KeyError:
                logger.exception("Unable to search", title)
                add_checked_item("Malformed search results from Radarr", ignored=None)
                continue
            add_attr(attrs, result, "originalLanguage")
            add_attr(attrs, result, "status")
            add_attr(attrs, result, "genres")
            attrs["year"] = result["year"]
            ratings = result.get("ratings")
            if ratings:
                attrs.update(get_ratings_attrs(ratings))
            # Use filter logic
            temp_item = IgnoreItem(item_type="mv", uid=imdb_id, attributes=attrs)
            ignore = should_ignore_by_rules(temp_item)
            poster = result.get("remotePoster")
            vec = None
            if ignore:
                enriched_attrs = attrs
                note = "Ignored by filter rules"
            else:
                enriched_attrs = annotate_with_ai(
                    "mv", imdb_id, result.get("title", normalized_title), attrs
                )
                note = "Flagged for review"
            try:
                plex_movie = find_movie(result["title"], result["year"])
            except Exception:
                logger.exception(
                    'Exception getting plex info for "%s" "%s"',
                    result["year"],
                    result["title"],
                )
                logger.info("New movie found %s", normalized_title)
                created = IgnoreItem.create(
                    title=normalized_title,
                    uid=imdb_id,
                    ignore=ignore,
                    shown=not ignore,
                    item_type="mv",
                    attributes=enriched_attrs,
                    poster_url=poster,
                )
            else:
                if not plex_movie:
                    logger.info("New movie found %s", normalized_title)
                    created = IgnoreItem.create(
                        title=normalized_title,
                        uid=imdb_id,
                        ignore=ignore,
                        shown=not ignore,
                        item_type="mv",
                        attributes=enriched_attrs,
                        poster_url=poster,
                    )
                    vec = enriched_attrs.pop("_synopsis_vector_tmp", None)
                    if vec is not None:
                        created.synopsis_vector = vec
                        created.save()
                else:
                    created = None
                    note = f"{note} (already in Plex)"

            add_checked_item(note, ignored=bool(ignore))
        summary = (
            f"Checked {len(checked_movies)} movie candidates from the last {days} days"
        )
        success = True
        logger.info(summary)
    except Exception as exc:
        error_details = (
            f"{type(exc).__name__}: {exc}. "
            "Confirm INDEXER_URL, API keys, and Radarr connectivity."
        )
        summary = f"Movie check for last {days} days failed"
        logger.exception(summary)
        raise
    finally:
        record_check_result(
            kind="movies",
            started_at=started_at,
            success=success,
            message=summary,
            checked_items=checked_movies,
            error_details=error_details,
        )


def get_show_titles() -> None:
    """uses tvdb_v4_api to get titles and posters for shows"""
    db = tvdb_v4_official.TVDB(TVDB_API_KEY)
    for item in IgnoreItem.filter(item_type="tv", ignore=False, checked_title=None):
        logger.info(f"Checking title {item.title}[{item.uid}]")
        try:
            series = db.get_series(item.uid)
            item.checked_title = f"{series['name']} ({series['year']})"
            img_url = series["image"]
            item.poster_url = (
                f"https://artworks.thetvdb.com/banners/{series['image']}"
                if img_url and "https://" not in img_url
                else img_url
            )
            # new logging for poster_url presence
            if not item.poster_url:
                logger.error(f"No poster_url for TV item {item.uid}")
            else:
                logger.info(
                    f"Saving poster_url {item.poster_url} for TV item {item.uid}"
                )
            item.save()
        except Exception:
            logger.exception(f"Unable to TVDB search for {item.uid}")


def get_movie_titles() -> None:
    ia = Cinemagoer()
    for item in IgnoreItem.filter(item_type="mv", ignore=False, checked_title=None):
        uid = item.uid[2:]
        logger.info(f"Checking title {item.title}[{item.uid}]")
        try:
            movie = ia.get_movie(uid)
            radarr_movie = radarr_query("movie/lookup", term="imdb:" + item.uid)[0]
            item.checked_title = f"{movie['title']} ({movie['year']})"
            item.poster_url = radarr_movie["remotePoster"]
            # new logging for poster_url presence
            if not item.poster_url:
                logger.error(f"No poster_url for movie item {item.uid}")
            else:
                logger.info(
                    f"Saving poster_url {item.poster_url} for movie item {item.uid}"
                )
            item.save()
            logger.info(f"Real title {movie['title']}")
        except Exception:
            logger.exception(f"Unable to IMDB search for {item.uid}")


def build_url(baseurl: str, **params: object) -> str:
    url_parts = list(urlparse.urlparse(baseurl))
    query = dict(urlparse.parse_qsl(url_parts[4]))
    query.update(params)  # type: ignore

    url_parts[4] = urlencode(query)

    return urlparse.urlunparse(url_parts)


def should_ignore(series: Dict[str, Any]) -> bool:
    if datetime.now().year - int(series["year"]) > 2:
        return True
    if "Reality" in series["genres"] or "Anime" in series["genres"]:
        return True
    return False


def add_attr(attrs: Dict[str, Any], show: Dict[str, Any], key: str) -> None:
    val = show.get(key)
    if not val:
        return
    if isinstance(val, list):
        attrs[key] = [str(x) for x in val]
    elif isinstance(val, dict):
        if "name" in val:
            attrs[key] = [str(val["name"])]
        else:
            attrs[key] = [str(x) for x in val.values()]
    else:
        attrs[key] = [str(val)]


def check_shows(days: int) -> None:
    started_at = datetime.utcnow()
    checked_shows = []
    success = False
    summary = "Show check did not complete"
    error_details = None
    context = {
        "r": config("INDEXER_APIKEY"),
        "num": config("INDEXER_NUM"),
        "days": days,
        "t": "5040",
        "dl": "1",
        "i": "84818",
    }
    for i in range(15):
        try:
            root = ET.fromstring(
                requests.get(config("INDEXER_URL"), params=context).text  # type: ignore
            )
            break
        except Exception:
            sleep(0.2)
    else:
        raise Exception("SSL attempts exhausted")
    shows = []
    checked_show_ids = set()

    def already_have(tvdb: str) -> bool:
        for show in shows:
            if show["tvdb"] == tvdb:
                return True
        return False

    reset_series()
    seen = set()

    try:
        for item in root.iter("item"):
            title = item.find("title").text  # type: ignore
            tvdb = get_attr(item, "tvdbid")
            if not tvdb:
                logger.error("no tvdb found for %s", title)
                continue
            if tvdb in seen:
                # cut down on log duplicates and api calls
                continue
            seen.add(tvdb)

            def add_checked_show(note: str, *, ignored: Optional[bool] = None) -> None:
                if tvdb in checked_show_ids:
                    return
                checked_show_ids.add(tvdb)
                checked_shows.append(
                    {
                        "title": title,
                        "uid": tvdb,
                        "ignored": ignored,
                        "note": note,
                    }
                )

            if IgnoreItem.exists("tv", tvdb):
                add_checked_show("Already recorded in ignore list")
                continue
            if already_have(tvdb):
                logger.debug("already have %s", title)
                add_checked_show("Duplicate in current feed")
                continue
            try:
                show = query_series(tvdb)
            except IndexError:
                logger.error("Unable to query series %s %s", tvdb, title)
                add_checked_show("Series query failed", ignored=None)
                continue
            attrs = get_attrs(item)
            attrs["year"] = show["year"]  # type: ignore
            tmdb_id = show.get("tmdbId") or get_tv_id(tvdb)
            if tmdb_id:
                attrs["tmdb_id"] = str(tmdb_id)
                try:
                    attrs["cast"] = get_tv_cast(tmdb_id, n=10)
                except Exception:
                    logger.exception("Unable to fetch cast for %s", title)
            ratings = show.get("ratings")
            if ratings:
                attrs["rating_votes"] = ratings["votes"]  # type: ignore
                attrs["rating_value"] = ratings["value"]  # type: ignore
            for key in ("network", "genres", "status", "seriesType", "certification"):
                add_attr(attrs, show, key)
            temp_item = IgnoreItem(item_type="tv", uid=tvdb, attributes=attrs)
            ignore = should_ignore_by_rules(temp_item)
            if ignore:
                enriched_attrs = attrs
                note = "Ignored by filter rules"
            else:
                enriched_attrs = annotate_with_ai("tv", tvdb, title, attrs)
                note = "Flagged for review"
            IgnoreItem.create(
                title=title,
                uid=tvdb,
                ignore=ignore,
                shown=not ignore,
                item_type="tv",
                attributes=enriched_attrs,
            )
            add_checked_show(note, ignored=bool(ignore))
        summary = (
            f"Checked {len(checked_shows)} show candidates from the last {days} days"
        )
        success = True
        logger.info(summary)
    except Exception as exc:
        error_details = (
            f"{type(exc).__name__}: {exc}. "
            "Confirm INDEXER_URL, API keys, and Sonarr connectivity."
        )
        summary = f"Show check for last {days} days failed"
        logger.exception(summary)
        raise
    finally:
        record_check_result(
            kind="shows",
            started_at=started_at,
            success=success,
            message=summary,
            checked_items=checked_shows,
            error_details=error_details,
        )


def movie_root_folder(folders: List[Dict[str, Any]]) -> Dict[str, Any]:
    for folder in folders:
        if folder.get("path", "") == "/mnt/syno4/Movies":
            return folder
    return folders[-1]


def addMovie(imdbId: str) -> None:
    result = radarr_query("movie/lookup/imdb", imdbid=imdbId)
    rootfolder = movie_root_folder(radarr_query("rootfolder"))
    data = result
    data["addOptions"] = {"searchForMovie": True}  # type: ignore
    data["rootFolderPath"] = rootfolder["path"]  # type: ignore
    data["qualityProfileId"] = 1  # type: ignore
    data["monitored"] = True  # type: ignore
    data["minimumAvailability"] = "announced"  # type: ignore
    data["id"] = 0  # type: ignore
    radarr_query("movie", method="post", **data)  # type: ignore
