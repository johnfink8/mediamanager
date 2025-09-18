import argparse
import logging

from sqlalchemy import Integer, and_, func, or_
from sqlalchemy.orm.attributes import flag_modified

from indexer_utils.models import FilterRule, IgnoreItem
from indexer_utils.session import db_session
from indexer_utils.tmdb import get_movie_cast, get_movie_id

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--type", type=str, default="mv")
    args = parser.parse_args()
    with db_session() as session:
        rules_q = session.query(FilterRule).filter_by(enabled=True)
        rules_q = rules_q.filter_by(item_type=args.type)
        rules = rules_q.all()
        disq_clauses = []
        for r in rules:
            # Map rule.attribute to column or JSON field
            if r.attribute in [
                "type",
                "uid",
                "title",
                "checked_title",
                "poster_url",
                "added",
                "ignore",
            ]:
                field = getattr(IgnoreItem, r.attribute)
            else:
                field = IgnoreItem.attributes[r.attribute]
            op = r.operator
            val = r.value
            cond = None
            if op == "eq":
                cond = field.contains(val)
            elif op == "neq":
                cond = ~field.contains(val)
            elif op == "in":
                values = [v.strip() for v in val.split(",")]
                cond = or_(*[field.contains(v) for v in values])
            elif op == "not_in":
                values = [v.strip() for v in val.split(",")]
                cond = and_(*[~field.contains(v) for v in values])
            elif op == "lt":
                cond = func.cast(field[0], Integer) < int(val)
            elif op == "gt":
                cond = func.cast(field[0], Integer) > int(val)
            elif op == "lte":
                cond = func.cast(field[0], Integer) <= int(val)
            elif op == "gte":
                cond = func.cast(field[0], Integer) >= int(val)
            elif op == "contains":
                cond = field.contains(val)
            elif op == "not_contains":
                cond = ~field.contains(val)
            else:
                continue
            disq_clauses.append(and_(IgnoreItem.item_type == r.item_type, cond))

        query = session.query(IgnoreItem)
        query = query.filter(IgnoreItem.item_type == args.type)
        query = query.filter(IgnoreItem.attributes["cast"].is_(None))
        if disq_clauses:
            query = query.filter(~or_(*disq_clauses))

        for item in query.limit(args.limit).all():
            if not item.attributes:
                item.attributes = {}
            if not item.attributes.get("tmdb_id"):
                movie_id = get_movie_id(item.uid)
                item.attributes["tmdb_id"] = movie_id
            else:
                movie_id = item.attributes["tmdb_id"]
            if movie_id:
                item.attributes["cast"] = get_movie_cast(movie_id, n=10)
            print(item.title, item.attributes.get("cast"))
            if not args.dry_run:
                flag_modified(item, "attributes")
                session.add(item)
                session.commit()


if __name__ == "__main__":
    main()
