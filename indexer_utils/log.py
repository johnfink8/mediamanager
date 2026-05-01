import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Generator, Optional

_item_ctx: ContextVar[Optional[str]] = ContextVar("item_ctx", default=None)


@contextmanager
def item_context(tag: str) -> Generator[None, None, None]:
    """Set per-item log tag for the current async task or thread."""
    token = _item_ctx.set(tag)
    try:
        yield
    finally:
        _item_ctx.reset(token)


class _TagFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        tag = _item_ctx.get()
        record.ctx_tag = f" [{tag}]" if tag else ""
        record.short_thread = (record.thread or 0) % 100000
        return True


_FMT = (
    "%(asctime)s [T:%(short_thread)05d] %(name)s %(levelname)s%(ctx_tag)s %(message)s"
)
_DATEFMT = "%H:%M:%S"


def configure_logging(level: int = logging.INFO) -> None:
    """Configure the root logger once at app startup."""
    handler = logging.StreamHandler()
    handler.addFilter(_TagFilter())
    handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)
