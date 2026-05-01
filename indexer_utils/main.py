import asyncio
import json
import logging
import os
import pathlib
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, PackageLoader, select_autoescape
from pydantic import BaseModel
from strawberry.fastapi import GraphQLRouter
from strawberry.subscriptions import GRAPHQL_TRANSPORT_WS_PROTOCOL, GRAPHQL_WS_PROTOCOL

from indexer_utils.vid_utils import (
    check_movies,
    check_shows,
    get_movie_titles,
    get_show_titles,
)

from .log import configure_logging
from .models import IgnoreItem
from .schema import events, schema

configure_logging()

env = Environment(loader=PackageLoader("indexer_utils"), autoescape=select_autoescape())
logger = logging.getLogger(__name__)

ROOT_DIR = pathlib.Path(__file__).parent.parent

app = FastAPI()
graphql_app = GraphQLRouter(
    schema,
    subscription_protocols=[
        GRAPHQL_TRANSPORT_WS_PROTOCOL,
        GRAPHQL_WS_PROTOCOL,
    ],
)
app.include_router(graphql_app, prefix="/graphql")


class InData(BaseModel):
    item_id: int


def load_scripts() -> str:
    try:
        with open(ROOT_DIR / "webpack-stats.json") as fp:
            wps = json.load(fp)
    except FileNotFoundError:
        logger.warning("webpack-stats.json not found; serving without bundled assets")
        return "<script></script>"
    templates = {
        ".js": '<script src="%s"></script>',
        ".css": '<link href="%s" rel="stylesheet" />',
    }
    files = []
    for chunk_set in wps["chunks"].values():
        for filename in chunk_set:
            ext = pathlib.Path(filename).suffix
            print(filename, ext)
            files.append(templates[ext] % wps["assets"][filename]["publicPath"])
    return "\n".join(files)


# helper to serialize SQLAlchemy model instances to dict
def item_to_dict(item: IgnoreItem) -> Dict[str, Any]:
    # use table columns to build dict
    return {col.name: getattr(item, col.name) for col in item.__table__.columns}


def _render_app() -> HTMLResponse:
    template = env.get_template("index.html")
    return template.render(SCRIPTS=load_scripts())


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return _render_app()


@app.get("/check_new/")
async def check_new() -> Dict[str, str]:
    # check_movies / check_shows internally use asyncio.run, which would
    # collide with the running FastAPI event loop. Offload to a worker
    # thread so each invocation gets its own asyncio context.
    logger.info("Checking movies")
    for i in (1, 4, 30):
        await asyncio.to_thread(check_movies, i)
    events.get("mv").set()
    logger.info("Checking shows")
    for i in (1, 4, 30):
        await asyncio.to_thread(check_shows, i)
    logger.info("Shows done")
    events.get("tv").set()
    return {"status": "done"}


@app.get("/check_titles/")
async def check_titles() -> Dict[str, str]:
    logger.info("Checking movies")
    get_movie_titles()
    events.get("mv").set()
    logger.info("checking shows")
    get_show_titles()
    events.get("tv").set()
    return {"status": "done"}


if os.environ.get("DEBUG"):
    app.mount("/static", StaticFiles(directory="frontend/static"), name="static")


@app.get("/{_path:path}", response_class=HTMLResponse)
async def spa_fallback(_path: str) -> HTMLResponse:
    return _render_app()
