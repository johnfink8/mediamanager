from decouple import config
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import Session

Base = declarative_base()


def get_db_url() -> str:
    return (
        f"mysql://{config('DB_USER')}:{config('DB_PASSWORD')}"
        + f"@{config('DB_HOST')}/{config('DB_NAME')}"
    )


def get_engine() -> Engine:
    return create_engine(get_db_url())


def db_session() -> Session:
    engine = get_engine()
    Base.metadata.create_all(engine)
    return Session(engine)
