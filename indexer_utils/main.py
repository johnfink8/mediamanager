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

from .models import IgnoreItem
from .schema import events, schema

env = Environment(loader=PackageLoader("indexer_utils"), autoescape=select_autoescape())
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.addHandler(logging.StreamHandler())

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
    wps = json.load(open(ROOT_DIR / "webpack-stats.json"))
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


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    template = env.get_template("index.html")
    return template.render(SCRIPTS=load_scripts())


@app.get("/check_new/")
async def check_new() -> Dict[str, str]:
    logger.info("Checking movies")
    for i in (1, 4, 30):
        check_movies(i)
    events.get("mv").set()
    logger.info("Checking shows")
    for i in (1, 4, 30):
        check_shows(i)
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
