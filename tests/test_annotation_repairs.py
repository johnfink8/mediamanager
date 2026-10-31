"""The scheduled repair pass for broken pending recommendations.

Selection (what counts as broken), per-item exponential backoff, and the
write-back rules — verdict on success, attempt counter on failure, user's
decision never clobbered. The agent itself is stubbed; its behaviour has its
own tests.
"""

import copy
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from sqlalchemy import select

from indexer_utils import repairs
from indexer_utils.models import IgnoreItem
from indexer_utils.session import db_session

FAILED_ATTRS: Dict[str, Any] = {
    "ai": {"failed": True, "value": None, "failure": {"code": "InternalServerError"}}
}
OK_ATTRS: Dict[str, Any] = {
    "ai": {"failed": False, "value": True, "score": 0.8, "reason": "great"}
}


def _seed(
    uid: str,
    *,
    attrs: Optional[Dict[str, Any]] = None,
    shown: bool = True,
    ignore: bool = False,
    added: bool = False,
    defer_until: Optional[datetime] = None,
    created_at: int = 1000,
) -> IgnoreItem:
    return IgnoreItem(
        item_type="mv",
        uid=uid,
        title=f"Title {uid}",
        shown=shown,
        ignore=ignore,
        added=added,
        defer_until=defer_until,
        created_at=created_at,
        # Deep copy: the repair pass stamps ai.repair into the outcome dict
        # in place; a shared module-level fixture would leak that stamp into
        # every later test (seeded items come back already "backed off").
        attributes=copy.deepcopy(attrs) if attrs is not None else None,
    )


async def _seed_all(*items: IgnoreItem) -> None:
    async with db_session() as session:
        for item in items:
            session.add(item)
        await session.commit()


async def _get(uid: str) -> IgnoreItem:
    async with db_session() as session:
        result = await session.execute(select(IgnoreItem).where(IgnoreItem.uid == uid))
        item = result.scalars().one()
        # Detach so attribute access survives the session closing.
        session.expunge(item)
        return item


def _with_repair(last_at: float, attempts: int = 1) -> Dict[str, Any]:
    ai = dict(FAILED_ATTRS["ai"], repair={"attempts": attempts, "last_at": last_at})
    return {"ai": ai}


async def test_find_broken_pending_selects_only_undecided_without_verdict() -> None:
    await _seed_all(
        _seed("failed", attrs=FAILED_ATTRS),
        _seed("missing-ai", attrs={}),
        _seed("null-attrs", attrs=None),
        _seed("healthy", attrs=OK_ATTRS),
        _seed("decided-ignore", attrs=FAILED_ATTRS, ignore=True),
        _seed("decided-added", attrs=FAILED_ATTRS, added=True),
        _seed("never-shown", attrs=FAILED_ATTRS, shown=False),
        _seed(
            "deferred",
            attrs=FAILED_ATTRS,
            defer_until=datetime.utcnow() + timedelta(hours=6),
        ),
    )
    async with db_session() as session:
        found = await repairs.find_broken_pending(session)
    assert {i.uid for i in found} == {"failed", "missing-ai", "null-attrs"}


async def test_find_orders_newest_first() -> None:
    await _seed_all(
        _seed("old", attrs=FAILED_ATTRS, created_at=100),
        _seed("new", attrs=FAILED_ATTRS, created_at=200),
    )
    async with db_session() as session:
        found = await repairs.find_broken_pending(session)
    assert [i.uid for i in found] == ["new", "old"]


def _stub_annotate(monkeypatch: Any, outcomes: Dict[str, Any]) -> Dict[str, list]:
    """Replace the agent with per-uid scripted results (dict / exception)."""
    seen: Dict[str, list] = {"uids": []}

    async def fake(item_type: str, uid: str, title: str, attrs: Dict[str, Any]) -> Any:
        seen["uids"].append(uid)
        outcome = outcomes[uid]
        if isinstance(outcome, BaseException):
            raise outcome
        # Fresh per call — the write-back stamps ai.repair into this dict.
        return copy.deepcopy(outcome)

    monkeypatch.setattr(repairs, "annotate_with_ai_async", fake)
    return seen


async def test_backoff_skips_recent_attempts_and_admits_expired_ones(
    monkeypatch: Any,
) -> None:
    now = time.time()
    await _seed_all(
        _seed("recent", attrs=_with_repair(now - 60)),
        _seed("expired", attrs=_with_repair(now - 2 * 3600)),
    )

    seen = _stub_annotate(monkeypatch, {"expired": dict(OK_ATTRS)})
    stats = await repairs.repair_broken_annotations()

    assert stats["found"] == 2
    assert stats["backed_off"] == 1
    assert stats["attempted"] == 1
    assert seen["uids"] == ["expired"]
    assert stats["repaired"] == 1

    item = await _get("expired")
    assert item.attributes["ai"]["failed"] is False
    assert "repair" not in item.attributes["ai"]  # healed — counter cleared


async def test_failed_repair_records_attempt_and_backs_off(monkeypatch: Any) -> None:
    await _seed_all(_seed("stubborn", attrs=FAILED_ATTRS))
    _stub_annotate(monkeypatch, {"stubborn": dict(FAILED_ATTRS)})

    stats = await repairs.repair_broken_annotations()
    assert stats["still_failed"] == 1
    item = await _get("stubborn")
    state = item.attributes["ai"]["repair"]
    assert state["attempts"] == 1
    assert time.time() - float(state["last_at"]) < 60

    # Second pass: inside the 1h backoff window — not attempted again.
    stats2 = await repairs.repair_broken_annotations()
    assert stats2["found"] == 1
    assert stats2["attempted"] == 0
    assert stats2["backed_off"] == 1


async def test_crashed_annotation_is_recorded_as_failure(monkeypatch: Any) -> None:
    await _seed_all(_seed("crasher", attrs=FAILED_ATTRS))
    _stub_annotate(monkeypatch, {"crasher": ConnectionError("db died mid-run")})

    stats = await repairs.repair_broken_annotations()
    assert stats["still_failed"] == 1
    item = await _get("crasher")
    ai = item.attributes["ai"]
    assert ai["failed"] is True
    assert ai["failure"]["code"] == "ConnectionError"
    assert ai["failure"]["stage"] == "repair"
    assert ai["repair"]["attempts"] == 1


async def test_decision_made_during_the_run_is_never_clobbered(
    monkeypatch: Any,
) -> None:
    await _seed_all(_seed("user-race", attrs=FAILED_ATTRS))

    # The user clicks Ignore while the agent is still running.
    async def decide_then_succeed(
        item_type: str, uid: str, title: str, attrs: Any
    ) -> Dict[str, Any]:
        async with db_session() as session:
            result = await session.execute(
                select(IgnoreItem).where(IgnoreItem.uid == uid)
            )
            row = result.scalars().one()
            row.ignore = True
            session.add(row)
            await session.commit()
        return copy.deepcopy(OK_ATTRS)

    monkeypatch.setattr(repairs, "annotate_with_ai_async", decide_then_succeed)
    stats = await repairs.repair_broken_annotations()

    assert stats["repaired"] == 0
    assert stats["decided_away"] == 1
    item = await _get("user-race")
    assert item.ignore is True
    # Untouched — no fresh verdict (or repair counter) written over the user.
    assert item.attributes == FAILED_ATTRS


def test_backoff_schedule() -> None:
    assert repairs.backoff_seconds(1) == 3600
    assert repairs.backoff_seconds(2) == 7200
    assert repairs.backoff_seconds(3) == 4 * 3600
    assert repairs.backoff_seconds(99) == repairs.MAX_BACKOFF_HOURS * 3600
