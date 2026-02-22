import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional, Type, Union

import strawberry
from sqlalchemy import Integer, and_, func, or_
from sqlalchemy.orm import Query
from sqlalchemy.orm.attributes import flag_modified
from strawberry.relay import GlobalID
from strawberry.scalars import ID, JSON

from indexer_utils.ai_recs import annotate_with_ai, refresh_visible_item_attributes
from indexer_utils.check_feedback import get_check_history
from indexer_utils.session import db_session
from indexer_utils.sonarr_utils import add_series
from indexer_utils.vid_utils import addMovie

from .models import (
    FilterRule,
    IgnoreItem,
    MovieRecommendationRecord,
    RecommendationPreference,
)
from .recommendations import MovieRecommendationResult, recommend_movie

logger = logging.getLogger(__name__)


@dataclass
class FilterSpec:
    model: Union[type[IgnoreItem], type[FilterRule]]
    field: str
    op: str
    value: str


def apply_filters(query: Query, spec: FilterSpec) -> Query:
    subfield_name = None
    print("spec", spec)
    if "." in spec.field:
        field_name, subfield_name = spec.field.split(".", 1)
    else:
        field_name = spec.field
    field = getattr(spec.model, field_name)
    if subfield_name:
        field = field[subfield_name]

    op = spec.op
    value = spec.value

    # Cross-database JSON filtering (works for both SQLite and MySQL):
    # - .contains(value) checks if value is present as a substring in any element of the list
    # - This is not an exact match, but works cross-db
    def _cast_json(element):
        if hasattr(element, "astext"):
            element = element.astext
        return func.cast(element, Integer)

    def _numeric_expr():
        raw_first = func.nullif(func.json_extract(field, "$[0]"), "null")
        raw_root = func.nullif(func.json_extract(field, "$"), "null")
        cast_first = func.cast(raw_first, Integer)
        cast_root = func.cast(raw_root, Integer)
        cast_direct = _cast_json(func.nullif(field, "null"))
        return func.coalesce(cast_first, cast_root, cast_direct)

    def _numeric_condition(operator: str, compare_value: int):
        numeric_value = _numeric_expr()
        if operator == "lt":
            return numeric_value < compare_value
        if operator == "gt":
            return numeric_value > compare_value
        if operator == "lte":
            return numeric_value <= compare_value
        if operator == "gte":
            return numeric_value >= compare_value
        raise ValueError(f"Unsupported numeric operator: {operator}")

    if op == "eq":
        query = query.where(field.contains(value))
    elif op == "neq":
        query = query.where(~field.contains(value))
    elif op == "in":
        values = [v.strip() for v in value.split(",")]
        query = query.where(or_(*[field.contains(v) for v in values]))
    elif op == "not_in":
        values = [v.strip() for v in value.split(",")]
        query = query.where(and_(*[~field.contains(v) for v in values]))
    elif op == "lt":
        query = query.where(field.is_not(None))
        query = query.where(_numeric_condition("lt", int(value)))
    elif op == "gt":
        query = query.where(field.is_not(None))
        query = query.where(_numeric_condition("gt", int(value)))
    elif op == "lte":
        query = query.where(field.is_not(None))
        query = query.where(_numeric_condition("lte", int(value)))
    elif op == "gte":
        query = query.where(field.is_not(None))
        query = query.where(_numeric_condition("gte", int(value)))
    elif op == "contains":
        query = query.where(field.contains(value))
    elif op == "not_contains":
        query = query.where(~field.contains(value))
    else:
        raise ValueError(f"Invalid operator: {spec.op}")
    return query


def invert_operator(op: str) -> str:
    mapping = {
        "eq": "not_contains",
        "neq": "contains",
        "in": "not_in",
        "not_in": "in",
        "lt": "gte",
        "lte": "gt",
        "gt": "lte",
        "gte": "lt",
        "contains": "not_contains",
        "not_contains": "contains",
    }
    if op not in mapping:
        raise ValueError(f"Cannot invert operator: {op}")
    return mapping[op]


events = {
    "tv": asyncio.Event(),
    "mv": asyncio.Event(),
}


@strawberry.type
class Attribute:
    name: str
    value: str


@strawberry.input
class AttributeInput:
    name: str
    value: str


@strawberry.input
class Filter:
    type: Optional[str] = None
    attribute: Optional[str] = None
    operator: Optional[str] = None
    value: Optional[str] = None
    # rule: Optional[FilterRule] = None  # Uncomment if you want to attach a FilterRule object


@strawberry.type
class AttributeEntry:
    key: str
    values: List[str]
    details: Optional[JSON] = None


RecommendationPreferenceEnum = strawberry.enum(
    RecommendationPreference, name="RecommendationPreference"
)


@strawberry.type
class CheckedItemType:
    title: str
    uid: str
    ignored: Optional[bool]
    note: Optional[str]


@strawberry.type
class CheckRunType:
    kind: str
    timestamp: str
    duration_ms: int
    success: bool
    message: str
    checked_count: int
    error: Optional[str]
    checked_items: List[CheckedItemType]


@strawberry.type
class CheckRunHistory:
    movies: List[CheckRunType]
    shows: List[CheckRunType]


def _check_run_from_dict(raw: Dict[str, Any]) -> CheckRunType:
    def _safe_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    items = [
        CheckedItemType(
            title=item.get("title", ""),
            uid=item.get("uid", ""),
            ignored=item.get("ignored"),
            note=item.get("note"),
        )
        for item in raw.get("checked_items", [])
        if isinstance(item, dict)
    ]

    return CheckRunType(
        kind=str(raw.get("kind", "")),
        timestamp=str(raw.get("timestamp", "")),
        duration_ms=_safe_int(raw.get("duration_ms", 0)),
        success=bool(raw.get("success", False)),
        message=str(raw.get("message", "")),
        checked_count=_safe_int(raw.get("checked_count", 0)),
        error=raw.get("error"),
        checked_items=items,
    )


@strawberry.type(name="IgnoreItem")
class IgnoreItemType:
    _id: strawberry.Private[int]
    type: str
    uid: str
    ignore: bool
    added: bool
    title: str
    checked_title: Optional[str]
    poster_url: Optional[str]
    attributes: List[AttributeEntry]

    @strawberry.field
    def id(self: "IgnoreItemType") -> GlobalID:
        return GlobalID("IgnoreItem", str(self._id))

    @classmethod
    def from_sqlalchemy(cls, item: "IgnoreItem") -> "IgnoreItemType":
        # Ensure attributes is always a List[AttributeEntry]
        attrs = item.attributes
        if attrs is None:
            attrs = {}
        normalized_attrs: Dict[str, List[str]] = {}
        attribute_entries: List[AttributeEntry] = []

        # Skip legacy AI keys if present; we will consolidate into single 'ai' entry
        legacy_ai_keys = {"ai_recommended", "ai_score", "ai_reason", "ai_similar_refs"}

        # If consolidated 'ai' dict is present, add it as a structured entry
        ai_obj = attrs.get("ai")
        if isinstance(ai_obj, dict):
            # Extract boolean value if provided, encode into values list as strings
            ai_value = ai_obj.get("value")
            if isinstance(ai_value, bool):
                ai_values = ["true" if ai_value else "false"]
            else:
                ai_values = []
            # Keep full details for on-demand UI
            attribute_entries.append(
                AttributeEntry(
                    key="ai",
                    values=ai_values,
                    details=ai_obj,
                )
            )

        for k, v in attrs.items():
            if k in legacy_ai_keys or k == "ai":
                continue
            # Decode bytes to str if needed
            if isinstance(k, bytes):
                k = k.decode("utf-8")
            if isinstance(v, bytes):
                v = v.decode("utf-8")
            if isinstance(v, list):
                normalized_attrs[str(k)] = [str(x) for x in v]
            elif isinstance(v, str):
                normalized_attrs[str(k)] = [v]
            elif v is None:
                normalized_attrs[str(k)] = []
            else:
                normalized_attrs[str(k)] = [str(v)]
        attribute_entries.extend(
            [AttributeEntry(key=k, values=v) for k, v in normalized_attrs.items()]
        )
        return cls(
            _id=int(item.id),  # type: ignore
            type=str(item.item_type),
            uid=str(item.uid),
            ignore=bool(item.ignore),
            added=bool(item.added),
            title=str(item.title),
            checked_title=(
                str(item.checked_title) if item.checked_title is not None else None
            ),
            poster_url=str(item.poster_url) if item.poster_url is not None else None,
            attributes=attribute_entries,
        )

    @staticmethod
    def apply_filter(filter: Filter, query: Query, cls=IgnoreItem) -> Query:
        if filter.type:
            query = query.where(cls.item_type == filter.type)
        return query

    @staticmethod
    def apply_filters(
        filters: Optional[List[Filter]], query: Query, cls=IgnoreItem
    ) -> Query:
        if filters:
            for filter in filters:
                query = IgnoreItemType.apply_filter(filter, query, cls=cls)
        return query

    @classmethod
    def get_open(
        cls: Type["IgnoreItemType"],
        filters: Optional[List[Filter]] = None,
    ) -> List["IgnoreItemType"]:
        with db_session() as session:
            query = session.query(IgnoreItem).where(
                IgnoreItem.ignore.is_(False),
                or_(
                    IgnoreItem.defer_until.is_(None),
                    IgnoreItem.defer_until <= datetime.utcnow(),
                ),
            )
            query = cls.apply_filters(filters, query)

            # Only apply temporary rules from filters argument
            temp_rules = []
            if filters:
                for f in filters:
                    if f.attribute and f.operator and f.value:
                        temp_rules.append(f)

            filter_specs = []
            # Add temporary rules
            for f in temp_rules:
                if f.attribute in [
                    "type",
                    "uid",
                    "title",
                    "checked_title",
                    "poster_url",
                    "added",
                    "ignore",
                ]:
                    field = f.attribute
                else:
                    field = f"attributes.{f.attribute}"
                filter_specs.append(
                    FilterSpec(
                        model=IgnoreItem,
                        field=field,
                        op=f.operator,
                        value=f.value,
                    )
                )
            for spec in filter_specs:
                query = apply_filters(query, spec)

            print(
                "query", query.statement.compile(compile_kwargs={"literal_binds": True})
            )
            items = query.all()
            return [cls.from_sqlalchemy(item) for item in items]


@strawberry.type
class IgnoreItemList:
    filters: strawberry.Private[Optional[List[Filter]]] = None

    @strawberry.field
    def id(self: "IgnoreItemList") -> GlobalID:
        items = [strawberry.asdict(f) for f in (self.filters or [])]
        filter_string = json.dumps(items)
        return GlobalID("ignoreitemlist", filter_string)

    @strawberry.field
    def nodes(self: "IgnoreItemList") -> List[IgnoreItemType]:
        return IgnoreItemType.get_open(filters=self.filters)


@strawberry.type(name="FilterRule")
class FilterRuleType:
    _id: strawberry.Private[int]
    item_type: str
    attribute: str
    operator: str
    value: str
    enabled: bool

    @strawberry.field
    def id(self: "FilterRuleType") -> GlobalID:
        return GlobalID("filterrule", str(self._id))

    @classmethod
    def from_sqlalchemy(cls, rule: FilterRule) -> "FilterRuleType":
        return cls(
            _id=int(rule.id),  # type: ignore
            item_type=str(rule.item_type),
            attribute=str(rule.attribute),
            operator=str(rule.operator),
            value=str(rule.value),
            enabled=bool(rule.enabled),
        )


@strawberry.input
class FilterRuleInput:
    item_type: str
    attribute: str
    operator: str
    value: str
    enabled: bool = True


@strawberry.type
class FilterRuleList:
    @strawberry.field
    def id(self) -> ID:
        # Use a static id for now; could be a hash of rules for more granularity
        return ID("filterrulelist")

    @strawberry.field
    def nodes(self) -> List[FilterRuleType]:
        with db_session() as session:
            rules = session.query(FilterRule).all()
            return [FilterRuleType.from_sqlalchemy(rule) for rule in rules]


@strawberry.type
class PageInfo:
    has_next_page: bool
    has_previous_page: bool
    start_offset: int
    end_offset: int
    total_count: int


@strawberry.type
class HistoricalIgnoreItemList:
    nodes: List[IgnoreItemType]
    page_info: PageInfo

    @staticmethod
    def get_historical(
        filters: Optional[List[Filter]] = None,
        limit: int = 20,
        offset: int = 0,
        apply_inverted_permanent_rules: bool = False,
    ) -> "HistoricalIgnoreItemList":
        with db_session() as session:
            query = session.query(IgnoreItem).where(IgnoreItem.ignore.is_(True))
            query = IgnoreItemType.apply_filters(filters, query)

            # Optionally apply the inverted permanent rules for this type
            if apply_inverted_permanent_rules:
                item_type_filter = next(
                    (f.type for f in (filters or []) if f.type is not None),
                    None,
                )
                if item_type_filter:
                    rules = (
                        session.query(FilterRule)
                        .filter_by(item_type=item_type_filter, enabled=True)
                        .all()
                    )
                    for rule in rules:
                        if rule.attribute in [
                            "type",
                            "uid",
                            "title",
                            "checked_title",
                            "poster_url",
                            "added",
                            "ignore",
                        ]:
                            field = rule.attribute
                        else:
                            field = f"attributes.{rule.attribute}"
                        spec = FilterSpec(
                            model=IgnoreItem,
                            field=field,
                            op=invert_operator(rule.operator),
                            value=rule.value,
                        )
                        query = apply_filters(query, spec)
            # Order by created_at DESC, then id DESC
            query = query.order_by(
                IgnoreItem.created_at.desc(),
                IgnoreItem.id.desc(),
            )
            total_count = query.count()
            items = query.offset(offset).limit(limit).all()
            nodes = [IgnoreItemType.from_sqlalchemy(item) for item in items]
            has_next_page = offset + limit < total_count
            has_previous_page = offset > 0
            page_info = PageInfo(
                has_next_page=has_next_page,
                has_previous_page=has_previous_page,
                start_offset=offset,
                end_offset=offset + len(nodes) - 1 if nodes else offset,
                total_count=total_count,
            )
            return HistoricalIgnoreItemList(nodes=nodes, page_info=page_info)


@strawberry.type
class MovieRecommendationType:
    id: Optional[ID]
    imdb_id: str
    title: str
    overview: Optional[str]
    poster_url: Optional[str]
    year: Optional[int]
    genres: List[str]
    cast: List[str]
    reason: Optional[str]
    source: str
    prompt: Optional[str]
    excluded_recent: List[str]
    preference: Optional[RecommendationPreferenceEnum]

    @classmethod
    def from_result(
        cls: type["MovieRecommendationType"],
        result: MovieRecommendationResult,
    ) -> "MovieRecommendationType":
        return cls(
            id=str(result.record_id) if result.record_id is not None else None,
            imdb_id=result.imdb_id,
            title=result.title,
            overview=result.overview,
            poster_url=result.poster_url,
            year=result.year,
            genres=result.genres,
            cast=result.cast,
            reason=result.reason,
            source=result.source,
            prompt=result.prompt,
            excluded_recent=result.excluded_recent,
            preference=result.preference,
        )


@strawberry.type
class SchemaQuery:
    @strawberry.field
    def items(
        self: "SchemaQuery", filters: Optional[List[Filter]] = None
    ) -> IgnoreItemList:
        return IgnoreItemList(filters=filters)

    @strawberry.field
    def filter_rules(self) -> FilterRuleList:
        return FilterRuleList()

    @strawberry.field
    def check_runs(self) -> CheckRunHistory:
        return CheckRunHistory(
            movies=[
                _check_run_from_dict(entry) for entry in get_check_history("movies")
            ],
            shows=[_check_run_from_dict(entry) for entry in get_check_history("shows")],
        )

    @strawberry.field
    def historical_items(
        self: "SchemaQuery",
        filters: Optional[List[Filter]] = None,
        limit: int = 20,
        offset: int = 0,
        apply_inverted_permanent_rules: bool = False,
    ) -> HistoricalIgnoreItemList:
        return HistoricalIgnoreItemList.get_historical(
            filters=filters,
            limit=limit,
            offset=offset,
            apply_inverted_permanent_rules=apply_inverted_permanent_rules,
        )

    @strawberry.field
    def movie_recommendation(
        self: "SchemaQuery", prompt: Optional[str] = None
    ) -> Optional[MovieRecommendationType]:
        result = recommend_movie(prompt)
        if result is None:
            return None
        return MovieRecommendationType.from_result(result)


@strawberry.type
class Subscription:
    @strawberry.subscription
    async def items(
        self: "Subscription", filters: Optional[List[Filter]]
    ) -> AsyncIterator[IgnoreItemList]:
        event_type = next((f.type for f in (filters or []) if f.type is not None), "tv")
        event = events.get(event_type or "tv")
        while True:
            if event is None:
                await asyncio.sleep(1)
                continue
            await event.wait()
            yield IgnoreItemList(filters=filters)
            event.clear()


@strawberry.input
class AddItemInput:
    id: GlobalID


@strawberry.input
class RetryAiInput:
    id: GlobalID


@strawberry.input
class DeferItemInput:
    id: GlobalID


@strawberry.type
class RecommendationFeedbackType:
    id: ID
    preference: RecommendationPreferenceEnum


@strawberry.input
class SetRecommendationPreferenceInput:
    recommendation_id: ID
    preference: RecommendationPreferenceEnum


@strawberry.type
class MutationReturn:
    ignore_items: IgnoreItemList
    filter_rules: FilterRuleList


@strawberry.type
class Mutation:
    @strawberry.mutation
    def add_item(self: "Mutation", data: AddItemInput) -> IgnoreItemType:
        with db_session() as session:
            item = session.query(IgnoreItem).get(data.id.node_id)
            if not item:
                raise Exception(f"Item not found: {data.id.node_id}")
            if item.item_type == "mv":
                addMovie(item.uid)
            else:
                add_series(item.uid)

            item.added = True
            item.ignore = True
            session.add(item)
            session.commit()
            return IgnoreItemType.from_sqlalchemy(item)

    @strawberry.mutation
    def delete_item(self: "Mutation", data: AddItemInput) -> IgnoreItemType:
        with db_session() as session:
            item = session.query(IgnoreItem).get(data.id.node_id)
            if not item:
                raise Exception("Item not found")
            item.ignore = True
            session.add(item)
            session.commit()
            return IgnoreItemType.from_sqlalchemy(item)

    @strawberry.input
    class SetItemAddedInput:
        id: GlobalID
        added: bool

    @strawberry.mutation
    def set_item_added(self: "Mutation", data: SetItemAddedInput) -> IgnoreItemType:
        with db_session() as session:
            item = session.query(IgnoreItem).get(data.id.node_id)
            if not item:
                raise Exception("Item not found")
            item.added = bool(data.added)
            session.add(item)
            session.commit()
            return IgnoreItemType.from_sqlalchemy(item)

    @strawberry.mutation
    def retry_ai(self: "Mutation", data: RetryAiInput) -> IgnoreItemType:
        with db_session() as session:
            item = session.query(IgnoreItem).get(data.id.node_id)
            if not item:
                raise Exception("Item not found")

            attrs = dict(item.attributes or {})
            # Clear any previous AI state before re-running
            attrs.pop("ai", None)
            attrs.pop("_synopsis_vector_tmp", None)

            refreshed_attrs = annotate_with_ai(
                item.item_type, item.uid, item.title, attrs
            )
            item.attributes = refreshed_attrs
            flag_modified(item, "attributes")
            session.add(item)
            session.commit()
            session.refresh(item)
            return IgnoreItemType.from_sqlalchemy(item)

    @strawberry.mutation
    def recheck_visible(self: "Mutation", item_type: str) -> List[IgnoreItemType]:
        with db_session() as session:
            now = datetime.utcnow()
            items = (
                session.query(IgnoreItem)
                .filter(
                    IgnoreItem.item_type == item_type,
                    IgnoreItem.ignore.is_(False),
                    or_(
                        IgnoreItem.defer_until.is_(None), IgnoreItem.defer_until <= now
                    ),
                )
                .all()
            )
            for item in items:
                attrs = refresh_visible_item_attributes(item)
                attrs.pop("ai", None)
                attrs.pop("_synopsis_vector_tmp", None)
                refreshed_attrs = annotate_with_ai(
                    item.item_type, item.uid, item.title, attrs
                )
                item.attributes = refreshed_attrs
                flag_modified(item, "attributes")
                session.add(item)
            session.commit()
            for item in items:
                session.refresh(item)
            return [IgnoreItemType.from_sqlalchemy(item) for item in items]

    @strawberry.mutation
    def defer_item(self: "Mutation", data: DeferItemInput) -> IgnoreItemType:
        with db_session() as session:
            item = session.query(IgnoreItem).get(data.id.node_id)
            if not item:
                raise Exception("Item not found")
            item.defer_until = datetime.utcnow() + timedelta(days=3)
            session.add(item)
            session.commit()
            return IgnoreItemType.from_sqlalchemy(item)

    @strawberry.mutation
    def set_recommendation_preference(
        self: "Mutation", data: SetRecommendationPreferenceInput
    ) -> RecommendationFeedbackType:
        try:
            record_id = int(str(data.recommendation_id))
        except (TypeError, ValueError):
            raise Exception("Invalid recommendation id")
        with db_session() as session:
            record = session.query(MovieRecommendationRecord).get(record_id)
            if record is None:
                raise Exception("Recommendation not found")
            record.preference = data.preference
            record.updated_at = int(datetime.utcnow().timestamp())
            session.add(record)
            session.commit()
            session.refresh(record)
            return RecommendationFeedbackType(
                id=str(record.id),
                preference=record.preference,
            )

    @strawberry.mutation
    def create_filter_rule(self, data: FilterRuleInput) -> MutationReturn:
        with db_session() as session:
            rule = FilterRule(
                item_type=data.item_type,
                attribute=data.attribute,
                operator=data.operator,
                value=data.value,
                enabled=data.enabled,
            )
            session.add(rule)
            session.commit()

            # Immediately apply the new rule to all matching IgnoreItems
            # Build a query for IgnoreItems of the correct type and not already ignored

            base_query = session.query(IgnoreItem).filter_by(
                item_type=data.item_type, ignore=False
            )
            # Build a FilterSpec for the new rule
            if data.attribute in [
                "type",
                "uid",
                "title",
                "checked_title",
                "poster_url",
                "added",
                "ignore",
            ]:
                field = data.attribute
            else:
                field = f"attributes.{data.attribute}"
            spec = FilterSpec(
                model=IgnoreItem,
                field=field,
                op=data.operator,
                value=data.value,
            )
            filtered_query = apply_filters(base_query, spec)
            for item in filtered_query.all():
                item.ignore = True
                session.add(item)
            session.commit()

            ignore_items = IgnoreItemList(filters=[Filter(type=data.item_type)])
            filter_rules = FilterRuleList()
            return MutationReturn(ignore_items=ignore_items, filter_rules=filter_rules)

    @strawberry.mutation
    def delete_filter_rule(self, id: GlobalID) -> MutationReturn:
        with db_session() as session:
            i_id = int(id.node_id)
            rule = session.query(FilterRule).get(i_id)
            if not rule:
                raise Exception("Rule not found")
            item_type = rule.item_type
            session.delete(rule)
            session.commit()
            ignore_items = IgnoreItemList(filters=[Filter(type=item_type)])
            filter_rules = FilterRuleList()
            return MutationReturn(ignore_items=ignore_items, filter_rules=filter_rules)


schema = strawberry.Schema(
    query=SchemaQuery, subscription=Subscription, mutation=Mutation
)
