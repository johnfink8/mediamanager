import argparse
import logging
from typing import Any, Dict

from sqlalchemy.orm.attributes import flag_modified

from indexer_utils.ai_recs import annotate_attributes_for_item
from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session
from indexer_utils.sonarr_utils import query_series
from indexer_utils.radarr_utils import radarr_query
from indexer_utils.vid_utils import add_attr, get_ratings_attrs


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())


def _build_movie_attributes(result: Dict[str, Any]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    add_attr(attrs, result, "originalLanguage")
    add_attr(attrs, result, "status")
    add_attr(attrs, result, "genres")
    if result.get("year") is not None:
        attrs["year"] = result.get("year")
    ratings = result.get("ratings")
    if ratings:
        attrs.update(get_ratings_attrs(ratings))
    return attrs


def _build_show_attributes(show: Dict[str, Any]) -> Dict[str, Any]:
    attrs: Dict[str, Any] = {}
    if show.get("year") is not None:
        attrs["year"] = show.get("year")
    ratings = show.get("ratings")
    if ratings:
        # Sonarr ratings format differs from Radarr; we mirror ingestion fields
        attrs["rating_votes"] = ratings.get("votes")
        attrs["rating_value"] = ratings.get("value")
    for key in ("network", "genres", "status", "seriesType", "certification"):
        add_attr(attrs, show, key)
    return attrs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--type",
        choices=["mv", "tv", "all"],
        default="all",
        help="Item type to backfill",
    )
    args = parser.parse_args()

    with db_session() as session:
        query = (
            session.query(IgnoreItem)
            .filter(IgnoreItem.attributes.is_(None))
            .order_by(IgnoreItem.created_at.desc())
        )
        if args.type != "all":
            query = query.filter(IgnoreItem.item_type == args.type)

        items = query.limit(args.limit).all()
        logger.info("Backfilling attributes for %d item(s)", len(items))

        for item in items:
            try:
                if item.item_type == "mv":
                    try:
                        result = radarr_query("movie/lookup", term="imdb:" + item.uid)[
                            0
                        ]
                    except Exception:
                        logger.exception(
                            "Unable to lookup movie %s [%s]", item.title, item.uid
                        )
                        continue
                    attrs = _build_movie_attributes(result)
                    title_for_ai = result.get("title") or item.title
                    if not item.ignore:
                        attrs = annotate_attributes_for_item(
                            "mv", item.uid, title_for_ai, attrs
                        )
                else:
                    try:
                        show = query_series(item.uid)
                    except Exception:
                        logger.exception(
                            "Unable to lookup series %s [%s]", item.title, item.uid
                        )
                        continue
                    attrs = _build_show_attributes(show)
                    if not item.ignore:
                        attrs = annotate_attributes_for_item(
                            "tv", item.uid, item.title, attrs
                        )

                logger.info(
                    "%s [%s]: attributes keys -> %s",
                    item.title,
                    item.uid,
                    sorted(list(attrs.keys())),
                )
                if not args.dry_run:
                    item.attributes = attrs
                    flag_modified(item, "attributes")
                    session.add(item)
                    session.commit()
            except Exception:
                logger.exception(
                    "Error backfilling item id=%s uid=%s", item.id, item.uid
                )


if __name__ == "__main__":
    main()
