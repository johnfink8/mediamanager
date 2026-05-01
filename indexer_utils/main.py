import asyncio
import json
import logging
import os
import pathlib
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, PackageLoader, select_autoescape
from pydantic import BaseModel
from strawberry.fastapi import GraphQLRouter
from strawberry.subscriptions import GRAPHQL_TRANSPORT_WS_PROTOCOL, GRAPHQL_WS_PROTOCOL

from .log import configure_logging
from .models import IgnoreItem
from .scheduler import (
    PLEX_SCAN_JOB_ID,
    shutdown_scheduler,
    start_scheduler,
    trigger_plex_scan_now,
)
from .schema import schema

configure_logging()

env = Environment(loader=PackageLoader("indexer_utils"), autoescape=select_autoescape())
logger = logging.getLogger(__name__)

ROOT_DIR = pathlib.Path(__file__).parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Boot the persistent APScheduler. The Plex scan job itself lives in
    # the SQLAlchemy jobstore (registered by Alembic migration
    # ``add_plex_scan_job``); the scheduler simply picks it up and honors
    # its persisted next_run_time, so a restart never causes an immediate
    # scan and parallel workers won't double-fire.
    start_scheduler()
    try:
        yield
    finally:
        shutdown_scheduler()


app = FastAPI(lifespan=lifespan)
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


@app.post("/admin/scan_plex/")
async def scan_plex_now() -> Dict[str, Any]:
    """Reschedule the persistent Plex scan job to fire immediately.

    Kept for shell-level access (curl). The Admin UI calls the
    ``triggerScheduledJob`` GraphQL mutation, which can trigger any of
    the registered jobs.
    """
    next_run = await asyncio.to_thread(trigger_plex_scan_now)
    if next_run is None:
        return {
            "status": "error",
            "detail": (
                f"Job {PLEX_SCAN_JOB_ID!r} not registered — apply the "
                "add_plex_scan_job Alembic migration."
            ),
        }
    return {"status": "scheduled", "job_id": PLEX_SCAN_JOB_ID, "next_run": next_run}


if os.environ.get("DEBUG"):
    app.mount("/static", StaticFiles(directory="frontend/static"), name="static")


@app.get("/{_path:path}", response_class=HTMLResponse)
async def spa_fallback(_path: str) -> HTMLResponse:
    return _render_app()
