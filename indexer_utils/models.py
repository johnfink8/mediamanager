import enum
from datetime import datetime
from typing import Any, List, Optional, Sequence, Type

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    Integer,
    String,
    Text,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from indexer_utils.session import Base, db_session

# OpenAI text-embedding-3-small dimensionality. Mirrors the constant in
# alembic/versions/add_pgvector_synopsis.py so the model and the
# migration stay in lockstep.
SYNOPSIS_VECTOR_DIMS = 1536


class IgnoreItem(Base):
    __tablename__ = "indexer_utils_ignoreitem"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_type: Mapped[str] = mapped_column(
        Enum("mv", "tv", name="type_choices"), nullable=False
    )
    uid: Mapped[str] = mapped_column(String(32), nullable=False)
    ignore: Mapped[bool] = mapped_column(Boolean, default=True)
    added: Mapped[bool] = mapped_column(Boolean, default=False)
    title: Mapped[str] = mapped_column(String(255), default="")
    checked_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    poster_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    attributes: Mapped[Optional[dict]] = mapped_column(JSONB(), nullable=True)
    created_at: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )  # Unix timestamp, default None
    shown: Mapped[bool] = mapped_column(Boolean, default=False)
    defer_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # Deferred: 6KB per row, only loaded when a search/index path asks for it.
    synopsis_vector: Mapped[Optional[Any]] = mapped_column(
        Vector(SYNOPSIS_VECTOR_DIMS), nullable=True, deferred=True
    )

    async def save(self) -> None:
        async with db_session() as session:
            await session.merge(self)
            await session.commit()

    @classmethod
    async def get_open(cls: Type["IgnoreItem"]) -> List["IgnoreItem"]:
        async with db_session() as session:
            now = datetime.utcnow()
            result = await session.execute(
                select(cls).where(
                    cls.ignore.is_(False),
                    or_(cls.defer_until.is_(None), cls.defer_until <= now),
                )
            )
            return list(result.scalars())

    @classmethod
    async def exists(cls: Type["IgnoreItem"], type: str, id: str) -> bool:
        async with db_session() as session:
            result = await session.execute(
                select(cls.id)
                .where(cls.item_type == type.lower(), cls.uid == id.lower())
                .limit(1)
            )
            return result.first() is not None

    @classmethod
    async def create(cls: Type["IgnoreItem"], **kwargs: object) -> "IgnoreItem":
        if "created_at" not in kwargs or kwargs["created_at"] is None:
            kwargs["created_at"] = int(datetime.now().timestamp())
        async with db_session() as session:
            item = cls(**kwargs)
            session.add(item)
            await session.commit()
            await session.refresh(item)
            return item

    @classmethod
    async def filter(cls: Type["IgnoreItem"], **kwargs: object) -> List["IgnoreItem"]:
        async with db_session() as session:
            result = await session.execute(select(cls).filter_by(**kwargs))
            return list(result.scalars())


class FilterRule(Base):
    __tablename__ = "indexer_utils_filterrule"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_type: Mapped[str] = mapped_column(
        Enum("mv", "tv", name="type_choices"), nullable=False
    )
    attribute: Mapped[str] = mapped_column(
        String(64), nullable=False
    )  # e.g., 'genre', 'publication_year'
    operator: Mapped[str] = mapped_column(
        String(16), nullable=False
    )  # e.g., 'eq', 'neq', 'lt', 'gt', 'in'
    value: Mapped[str] = mapped_column(
        String(255), nullable=False
    )  # value to compare against (as string)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    async def save(self) -> None:
        async with db_session() as session:
            await session.merge(self)
            await session.commit()


class RecommendationPreference(enum.Enum):
    LIKE = "LIKE"
    NOT_NOW = "NOT_NOW"
    NEVER = "NEVER"


class MovieRecommendationRecord(Base):
    __tablename__ = "movie_recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recommended_imdb_id: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )
    recommended_title: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    recommended_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    source: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    preference: Mapped[Optional[RecommendationPreference]] = mapped_column(
        Enum(RecommendationPreference, name="movie_recommendation_preference"),
        nullable=True,
    )
    created_at: Mapped[int] = mapped_column(
        Integer, default=lambda: int(datetime.utcnow().timestamp())
    )
    updated_at: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    async def save(self) -> None:
        async with db_session() as session:
            await session.merge(self)
            await session.commit()

    async def set_preference(self, preference: RecommendationPreference) -> None:
        self.preference = preference
        self.updated_at = int(datetime.utcnow().timestamp())
        await self.save()

    @classmethod
    async def log_recommendation(
        cls: Type["MovieRecommendationRecord"],
        *,
        prompt: Optional[str],
        imdb_id: Optional[str],
        title: Optional[str],
        reason: Optional[str],
        source: Optional[str],
    ) -> "MovieRecommendationRecord":
        async with db_session() as session:
            record = cls(
                prompt=prompt,
                recommended_imdb_id=imdb_id,
                recommended_title=title,
                recommended_reason=reason,
                source=source,
                created_at=int(datetime.utcnow().timestamp()),
            )
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record

    @classmethod
    async def get_by_id(cls, record_id: int) -> Optional["MovieRecommendationRecord"]:
        async with db_session() as session:
            return await session.get(cls, record_id)

    @classmethod
    async def recent_history(
        cls, limit: int = 10
    ) -> Sequence["MovieRecommendationRecord"]:
        async with db_session() as session:
            result = await session.execute(
                select(cls).order_by(cls.created_at.desc(), cls.id.desc()).limit(limit)
            )
            return list(result.scalars())
