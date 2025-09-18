from datetime import datetime
from typing import Iterable, List, Optional, Type

from sqlalchemy import (
    JSON,
    Boolean,
    Enum,
    Integer,
    String,
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
