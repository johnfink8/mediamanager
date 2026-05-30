"""Async client for the vdiag ffmpeg sidecar.

Mirrors the radarr/sonarr idiom: a synchronous ``requests`` call wrapped in
``asyncio.to_thread`` so the app keeps no extra async-HTTP dependency. The
sidecar is internal-only and gated by ``VDIAG_TOKEN``.

Calls name a Plex ratingKey, never a path — vdiag resolves the on-disk file
from Plex itself so the app can't (and doesn't) hand it an arbitrary path.

``probe`` is quick and returns synchronously. ``scan`` and ``remux`` are slow,
so they're fire-and-poll: the kickoff returns a ``job_id`` immediately and the
job's progress/result lands in Redis under ``vdiag:job:{id}``, read via
``aget_job``.
"""

import asyncio
from typing import Any, Dict, Optional

import requests
from decouple import config

from indexer_utils.redis_client import get_redis_client, redis_get_json

# All vdiag endpoints respond fast now (probe runs inline; scan/remux only
# enqueue a job), so a short timeout is fine.
_TIMEOUT = 60
_JOB_KEY = "vdiag:job:{}"  # must match vdiag/server.py


class VdiagError(RuntimeError):
    """A vdiag call returned an error. Carries vdiag's own (clean) detail so the
    MCP layer can forward it to the model as an actionable message."""


def _post(endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    url = "/".join([config("VDIAG_URL").rstrip("/"), endpoint])
    resp = requests.post(
        url,
        json=payload,
        headers={"X-Vdiag-Token": config("VDIAG_TOKEN")},
        timeout=_TIMEOUT,
    )
    if resp.status_code >= 400:
        raise VdiagError(f"vdiag {endpoint} failed ({resp.status_code}): {resp.text}")
    result: Dict[str, Any] = resp.json()
    return result


async def aprobe(rating_key: str) -> Dict[str, Any]:
    return await asyncio.to_thread(_post, "probe", {"rating_key": rating_key})


async def astart_scan(
    rating_key: str, duration: Optional[float] = None
) -> Dict[str, Any]:
    """Start a background decode scan; returns ``{job_id, status}``."""
    return await asyncio.to_thread(
        _post, "scan", {"rating_key": rating_key, "duration": duration}
    )


async def astart_remux(rating_key: str) -> Dict[str, Any]:
    """Start a background lossless remux; returns ``{job_id, status}``."""
    return await asyncio.to_thread(_post, "remux", {"rating_key": rating_key})


async def aget_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Read a vdiag job's state from Redis (None if unknown/expired)."""

    def _read() -> Optional[Dict[str, Any]]:
        result = redis_get_json(get_redis_client(), _JOB_KEY.format(job_id))
        if result is None:
            return None
        assert isinstance(result, dict)
        return result

    return await asyncio.to_thread(_read)
