import enum
from datetime import datetime
from typing import Iterable, List, Optional, Sequence, Type

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from indexer_utils.session import Base, db_session


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
    attributes: Mapped[Optional[dict]] = mapped_column(JSON, nullable=True)
    created_at: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True
    )  # Unix timestamp, default None

    def save(self) -> None:
        session = Session.object_session(self)
        if session is None:
            session = db_session()
            session.add(self)
        session.commit()

    @classmethod
    def get_open(cls: Type["IgnoreItem"]) -> List["IgnoreItem"]:
        session = db_session()
        items = session.query(cls).filter_by(ignore=False)
        return list(items)

    @classmethod
    def exists(cls: Type["IgnoreItem"], type: str, id: str) -> bool:
        session = db_session()
        return any(session.query(cls).filter_by(item_type=type.lower(), uid=id.lower()))

    @classmethod
    def create(cls: Type["IgnoreItem"], **kwargs: object) -> "IgnoreItem":
        if "created_at" not in kwargs or kwargs["created_at"] is None:
            kwargs["created_at"] = int(datetime.now().timestamp())
        session = db_session()
        item = cls(**kwargs)
        session.add(item)
        session.commit()
        return item

    @classmethod
    def filter(cls: Type["IgnoreItem"], **kwargs: object) -> Iterable["IgnoreItem"]:
        session = db_session()
        return session.query(cls).filter_by(**kwargs)


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
    # Optionally: user_id = mapped_column(Integer, nullable=True)

    def save(self) -> None:
        session = Session.object_session(self)
        if session is None:
            session = db_session()
            session.add(self)
        session.commit()


class RecommendationPreference(enum.Enum):
    LIKE = "LIKE"
    NOT_NOW = "NOT_NOW"
    NEVER = "NEVER"


class MovieRecommendationRecord(Base):
    __tablename__ = "movie_recommendations"

    id: Mapped[int] = mapped_column(primary_key=True)
    prompt: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recommended_imdb_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
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

    def save(self) -> None:
        session = Session.object_session(self)
        if session is None:
            session = db_session()
            session.add(self)
        session.commit()

    def set_preference(self, preference: RecommendationPreference) -> None:
        session = Session.object_session(self)
        if session is None:
            session = db_session()
            session.add(self)
        self.preference = preference
        self.updated_at = int(datetime.utcnow().timestamp())
        session.commit()

    @classmethod
    def log_recommendation(
        cls: Type["MovieRecommendationRecord"],
        *,
        prompt: Optional[str],
        imdb_id: Optional[str],
        title: Optional[str],
        reason: Optional[str],
        source: Optional[str],
    ) -> "MovieRecommendationRecord":
        session = db_session()
        record = cls(
            prompt=prompt,
            recommended_imdb_id=imdb_id,
            recommended_title=title,
            recommended_reason=reason,
            source=source,
            created_at=int(datetime.utcnow().timestamp()),
        )
        session.add(record)
        session.commit()
        session.refresh(record)
        return record

    @classmethod
    def get_by_id(cls, record_id: int) -> Optional["MovieRecommendationRecord"]:
        session = db_session()
        return session.query(cls).get(record_id)

    @classmethod
    def recent_history(
        cls, limit: int = 10
    ) -> Sequence["MovieRecommendationRecord"]:
        session = db_session()
        return (
            session.query(cls)
            .order_by(cls.created_at.desc(), cls.id.desc())
            .limit(limit)
            .all()
        )
