import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, AsyncIterator, Dict, List, Optional, Type

import strawberry
from sqlalchemy import func, or_, select
from sqlalchemy.orm.attributes import flag_modified
from strawberry.relay import GlobalID
from strawberry.scalars import ID, JSON

from indexer_utils.ai_recs import (
    annotate_with_ai_async,
    refresh_visible_item_attributes,
)
from indexer_utils.check_feedback import get_check_history
from indexer_utils.session import db_session
from indexer_utils.sonarr_utils import add_series
from indexer_utils.vid_utils import addMovie

from .models import (
    IgnoreItem,
    MovieRecommendationRecord,
    RecommendationPreference,
)
from .recommendations import MovieRecommendationResult, recommend_movie

logger = logging.getLogger(__name__)


events = {
    "tv": asyncio.Event(),
    "mv": asyncio.Event(),
}


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
    created_at: Optional[int]

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
            created_at=int(item.created_at) if item.created_at is not None else None,
        )

    @classmethod
    async def get_open(
        cls: Type["IgnoreItemType"],
        item_type: Optional[str] = None,
    ) -> List["IgnoreItemType"]:
        async with db_session() as session:
            stmt = select(IgnoreItem).where(
                IgnoreItem.ignore.is_(False),
                or_(
                    IgnoreItem.defer_until.is_(None),
                    IgnoreItem.defer_until <= datetime.utcnow(),
                ),
            )
            if item_type:
                stmt = stmt.where(IgnoreItem.item_type == item_type)
            items = list((await session.execute(stmt)).scalars())

            def sort_key(item: IgnoreItem) -> "tuple[int, float, float]":
                score = ((item.attributes or {}).get("ai") or {}).get("score")
                has_score = isinstance(score, (int, float))
                created = float(item.created_at or float("inf"))
                return (
                    0 if has_score else 1,
                    -(float(score) if has_score and score is not None else 0.0),
                    created,
                )

            items.sort(key=sort_key)
            return [cls.from_sqlalchemy(item) for item in items]


@strawberry.type
class IgnoreItemList:
    item_type: strawberry.Private[Optional[str]] = None

    @strawberry.field
    def id(self: "IgnoreItemList") -> GlobalID:
        return GlobalID("ignoreitemlist", self.item_type or "")

    @strawberry.field
    async def nodes(self: "IgnoreItemList") -> List[IgnoreItemType]:
        return await IgnoreItemType.get_open(item_type=self.item_type)


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
    async def get_historical(
        item_type: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        search: Optional[str] = None,
    ) -> "HistoricalIgnoreItemList":
        async with db_session() as session:
            query = select(IgnoreItem).where(IgnoreItem.ignore.is_(True))
            if item_type:
                query = query.where(IgnoreItem.item_type == item_type)

            if search and search.strip():
                pattern = f"%{search.strip()}%"
                query = query.where(
                    or_(
                        IgnoreItem.title.ilike(pattern),
                        IgnoreItem.checked_title.ilike(pattern),
                    )
                )

            # Order by created_at DESC, then id DESC
            query = query.order_by(
                IgnoreItem.created_at.desc(),
                IgnoreItem.id.desc(),
            )
            total_count = (
                await session.execute(
                    select(func.count()).select_from(query.subquery())
                )
            ).scalar_one()
            items = list(
                (await session.execute(query.offset(offset).limit(limit))).scalars()
            )
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


# ---------------------------------------------------------------------------
# Admin: APScheduler job management
# ---------------------------------------------------------------------------


@strawberry.type
class ScheduledJobTriggerType:
    kind: str
    expression: str
    fields: JSON


@strawberry.type
class ScheduledJobType:
    id: ID
    name: str
    description: str
    next_run_time: Optional[str]
    paused: bool
    trigger: ScheduledJobTriggerType

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduledJobType":
        trig = data["trigger"]
        return cls(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            next_run_time=data["next_run_time"],
            paused=data["paused"],
            trigger=ScheduledJobTriggerType(
                kind=trig["kind"],
                expression=trig["expression"],
                fields=trig["fields"],
            ),
        )


@strawberry.input
class ScheduledJobCronInput:
    year: Optional[str] = None
    month: Optional[str] = None
    day: Optional[str] = None
    week: Optional[str] = None
    day_of_week: Optional[str] = None
    hour: Optional[str] = None
    minute: Optional[str] = None
    second: Optional[str] = None


@strawberry.input
class ScheduledJobIntervalInput:
    weeks: Optional[int] = None
    days: Optional[int] = None
    hours: Optional[int] = None
    minutes: Optional[int] = None
    seconds: Optional[int] = None


@strawberry.input
class UpdateScheduledJobTriggerInput:
    id: ID
    kind: str
    cron: Optional[ScheduledJobCronInput] = None
    interval: Optional[ScheduledJobIntervalInput] = None


@strawberry.type
class SchemaQuery:
    @strawberry.field
    def items(self: "SchemaQuery", item_type: Optional[str] = None) -> IgnoreItemList:
        return IgnoreItemList(item_type=item_type)

    @strawberry.field
    def check_runs(self) -> CheckRunHistory:
        return CheckRunHistory(
            movies=[
                _check_run_from_dict(entry) for entry in get_check_history("movies")
            ],
            shows=[_check_run_from_dict(entry) for entry in get_check_history("shows")],
        )

    @strawberry.field
    async def historical_items(
        self: "SchemaQuery",
        item_type: Optional[str] = None,
        limit: int = 20,
        offset: int = 0,
        search: Optional[str] = None,
    ) -> HistoricalIgnoreItemList:
        return await HistoricalIgnoreItemList.get_historical(
            item_type=item_type,
            limit=limit,
            offset=offset,
            search=search,
        )

    @strawberry.field
    def scheduled_jobs(self: "SchemaQuery") -> List[ScheduledJobType]:
        from indexer_utils.scheduler import list_scheduled_jobs

        return [ScheduledJobType.from_dict(d) for d in list_scheduled_jobs()]

    @strawberry.field
    async def movie_recommendation(
        self: "SchemaQuery", prompt: Optional[str] = None
    ) -> Optional[MovieRecommendationType]:
        result = await recommend_movie(prompt)
        if result is None:
            return None
        return MovieRecommendationType.from_result(result)


@strawberry.type
class Subscription:
    @strawberry.subscription
    async def items(
        self: "Subscription", item_type: Optional[str] = None
    ) -> AsyncIterator[IgnoreItemList]:
        event = events.get(item_type or "tv")
        while True:
            if event is None:
                await asyncio.sleep(1)
                continue
            await event.wait()
            yield IgnoreItemList(item_type=item_type)
            event.clear()


@strawberry.input
class AddItemInput:
    id: GlobalID


@strawberry.input
class AcceptAllRecommendedInput:
    ids: List[GlobalID]
    item_type: str


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
class AcceptAllRecommendedResult:
    added_count: int
    ignored_count: int
    items: IgnoreItemList


@strawberry.type
class Mutation:
    @strawberry.mutation
    async def add_item(self: "Mutation", data: AddItemInput) -> IgnoreItemType:
        async with db_session() as session:
            item = await session.get(IgnoreItem, data.id.node_id)
            if not item:
                raise Exception(f"Item not found: {data.id.node_id}")
            if item.item_type == "mv":
                addMovie(item.uid)
            else:
                add_series(item.uid)

            item.added = True
            item.ignore = True
            session.add(item)
            await session.commit()
            return IgnoreItemType.from_sqlalchemy(item)

    @strawberry.mutation
    async def accept_all_recommended(
        self: "Mutation", data: AcceptAllRecommendedInput
    ) -> AcceptAllRecommendedResult:
        added_count = 0
        ignored_count = 0
        for gid in data.ids:
            try:
                async with db_session() as session:
                    item = await session.get(IgnoreItem, gid.node_id)
                    if not item:
                        continue
                    ai = (item.attributes or {}).get("ai", {})
                    if ai.get("value") is not True:
                        item.ignore = True
                        session.add(item)
                        await session.commit()
                        ignored_count += 1
                        continue
                    if item.item_type == "mv":
                        addMovie(item.uid)
                    else:
                        add_series(item.uid)
                    item.added = True
                    item.ignore = True
                    session.add(item)
                    await session.commit()
                    added_count += 1
            except Exception:
                logger.exception("Failed to add item %s", gid)
        return AcceptAllRecommendedResult(
            added_count=added_count,
            ignored_count=ignored_count,
            items=IgnoreItemList(item_type=data.item_type),
        )

    @strawberry.mutation
    async def delete_item(self: "Mutation", data: AddItemInput) -> IgnoreItemType:
        async with db_session() as session:
            item = await session.get(IgnoreItem, data.id.node_id)
            if not item:
                raise Exception("Item not found")
            item.ignore = True
            session.add(item)
            await session.commit()
            return IgnoreItemType.from_sqlalchemy(item)

    @strawberry.input
    class SetItemAddedInput:
        id: GlobalID
        added: bool

    @strawberry.mutation
    async def set_item_added(
        self: "Mutation", data: SetItemAddedInput
    ) -> IgnoreItemType:
        async with db_session() as session:
            item = await session.get(IgnoreItem, data.id.node_id)
            if not item:
                raise Exception("Item not found")
            item.added = bool(data.added)
            session.add(item)
            await session.commit()
            return IgnoreItemType.from_sqlalchemy(item)

    @strawberry.mutation
    async def retry_ai(self: "Mutation", data: RetryAiInput) -> IgnoreItemType:
        # Read snapshot, drop the session, run the agent (don't pin a
        # connection across an await boundary), then write results back.
        async with db_session() as session:
            item = await session.get(IgnoreItem, data.id.node_id)
            if not item:
                raise Exception("Item not found")
            item_type = item.item_type
            uid = item.uid
            title = item.title
            attrs = dict(item.attributes or {})
            attrs.pop("ai", None)
            attrs.pop("_synopsis_vector_tmp", None)

        refreshed_attrs = await annotate_with_ai_async(item_type, uid, title, attrs)

        async with db_session() as session:
            item = await session.get(IgnoreItem, data.id.node_id)
            if not item:
                raise Exception("Item not found")
            item.attributes = refreshed_attrs
            flag_modified(item, "attributes")
            session.add(item)
            await session.commit()
            await session.refresh(item)
            return IgnoreItemType.from_sqlalchemy(item)

    @strawberry.mutation
    async def recheck_visible(self: "Mutation", item_type: str) -> List[IgnoreItemType]:
        # Phase 1: read items + refresh metadata.
        async with db_session() as session:
            now = datetime.utcnow()
            result = await session.execute(
                select(IgnoreItem).where(
                    IgnoreItem.item_type == item_type,
                    IgnoreItem.ignore.is_(False),
                    or_(
                        IgnoreItem.defer_until.is_(None),
                        IgnoreItem.defer_until <= now,
                    ),
                )
            )
            items = list(result.scalars())
            prepared: List[Dict[str, Any]] = []
            for item in items:
                attrs = refresh_visible_item_attributes(item)
                attrs.pop("ai", None)
                attrs.pop("_synopsis_vector_tmp", None)
                prepared.append(
                    {
                        "id": item.id,
                        "item_type": item.item_type,
                        "uid": item.uid,
                        "title": item.title,
                        "attrs": attrs,
                    }
                )
            ordered_ids = [p["id"] for p in prepared]

        if not prepared:
            return []

        # Phase 2: run agent for each item, gated by AI_ANNOTATE_CONCURRENCY.
        from indexer_utils.vid_utils import AI_ANNOTATE_CONCURRENCY

        semaphore = asyncio.Semaphore(AI_ANNOTATE_CONCURRENCY)

        async def _annotate(p: Dict[str, Any]) -> Dict[str, Any]:
            async with semaphore:
                return await annotate_with_ai_async(
                    p["item_type"], p["uid"], p["title"], p["attrs"]
                )

        results = await asyncio.gather(
            *(_annotate(p) for p in prepared), return_exceptions=True
        )

        # Phase 3: write enriched attrs back.
        async with db_session() as session:
            for p, outcome in zip(prepared, results):
                if isinstance(outcome, BaseException):
                    logger.exception(
                        "annotate failed for %s:%s", p["item_type"], p["uid"]
                    )
                    continue
                item = await session.get(IgnoreItem, p["id"])
                if item is None:
                    continue
                item.attributes = outcome
                flag_modified(item, "attributes")
                session.add(item)
            await session.commit()
            ordered = list(
                (
                    await session.execute(
                        select(IgnoreItem).where(IgnoreItem.id.in_(ordered_ids))
                    )
                ).scalars()
            )
            refreshed_items: List[IgnoreItem] = sorted(
                ordered, key=lambda r: ordered_ids.index(r.id)
            )
            return [IgnoreItemType.from_sqlalchemy(it) for it in refreshed_items]

    @strawberry.mutation
    async def defer_item(self: "Mutation", data: DeferItemInput) -> IgnoreItemType:
        async with db_session() as session:
            item = await session.get(IgnoreItem, data.id.node_id)
            if not item:
                raise Exception("Item not found")
            item.defer_until = datetime.utcnow() + timedelta(days=3)
            session.add(item)
            await session.commit()
            return IgnoreItemType.from_sqlalchemy(item)

    @strawberry.mutation
    async def set_recommendation_preference(
        self: "Mutation", data: SetRecommendationPreferenceInput
    ) -> RecommendationFeedbackType:
        try:
            record_id = int(str(data.recommendation_id))
        except (TypeError, ValueError):
            raise Exception("Invalid recommendation id")
        async with db_session() as session:
            record = await session.get(MovieRecommendationRecord, record_id)
            if record is None:
                raise Exception("Recommendation not found")
            record.preference = data.preference
            record.updated_at = int(datetime.utcnow().timestamp())
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return RecommendationFeedbackType(
                id=str(record.id),
                preference=record.preference,
            )

    # ----- Admin: APScheduler job management -----

    @strawberry.mutation
    def trigger_scheduled_job(self, id: ID) -> ScheduledJobType:
        from indexer_utils.scheduler import trigger_job_now

        result = trigger_job_now(str(id))
        if result is None:
            raise Exception(f"Scheduled job not found: {id}")
        return ScheduledJobType.from_dict(result)

    @strawberry.mutation
    def pause_scheduled_job(self, id: ID) -> ScheduledJobType:
        from indexer_utils.scheduler import pause_job

        result = pause_job(str(id))
        if result is None:
            raise Exception(f"Scheduled job not found: {id}")
        return ScheduledJobType.from_dict(result)

    @strawberry.mutation
    def resume_scheduled_job(self, id: ID) -> ScheduledJobType:
        from indexer_utils.scheduler import resume_job

        result = resume_job(str(id))
        if result is None:
            raise Exception(f"Scheduled job not found: {id}")
        return ScheduledJobType.from_dict(result)

    @strawberry.mutation
    def update_scheduled_job_trigger(
        self, data: UpdateScheduledJobTriggerInput
    ) -> ScheduledJobType:
        from indexer_utils.scheduler import update_job_trigger

        cron_args: Optional[Dict[str, str]] = None
        if data.cron is not None:
            cron_args = {
                k: v
                for k, v in {
                    "year": data.cron.year,
                    "month": data.cron.month,
                    "day": data.cron.day,
                    "week": data.cron.week,
                    "day_of_week": data.cron.day_of_week,
                    "hour": data.cron.hour,
                    "minute": data.cron.minute,
                    "second": data.cron.second,
                }.items()
                if v is not None and v != ""
            }
        interval_args: Optional[Dict[str, int]] = None
        if data.interval is not None:
            interval_args = {
                k: v
                for k, v in {
                    "weeks": data.interval.weeks,
                    "days": data.interval.days,
                    "hours": data.interval.hours,
                    "minutes": data.interval.minutes,
                    "seconds": data.interval.seconds,
                }.items()
                if v is not None
            }
        result = update_job_trigger(
            str(data.id), kind=data.kind, cron=cron_args, interval=interval_args
        )
        if result is None:
            raise Exception(f"Scheduled job not found: {data.id}")
        return ScheduledJobType.from_dict(result)


schema = strawberry.Schema(
    query=SchemaQuery, subscription=Subscription, mutation=Mutation
)
