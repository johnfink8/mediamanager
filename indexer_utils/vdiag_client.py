"""Async client for the vdiag ffmpeg sidecar.

Mirrors the radarr/sonarr idiom: a synchronous ``requests`` call wrapped in
``asyncio.to_thread`` so the app keeps no extra async-HTTP dependency. The
sidecar is internal-only and gated by ``VDIAG_TOKEN``; scan/remux can run for
minutes, so the timeouts are generous.

Calls name a Plex ratingKey, never a path — vdiag resolves the on-disk file
from Plex itself so the app can't (and doesn't) hand it an arbitrary path.
"""

import asyncio
from typing import Any, Dict, Optional

import requests
from decouple import config

# Probe is quick; scan/remux decode or rewrite the whole file.
_PROBE_TIMEOUT = 60
_HEAVY_TIMEOUT = 60 * 30


def _post(endpoint: str, payload: Dict[str, Any], timeout: int) -> Dict[str, Any]:
    url = "/".join([config("VDIAG_URL").rstrip("/"), endpoint])
    resp = requests.post(
        url,
        json=payload,
        headers={"X-Vdiag-Token": config("VDIAG_TOKEN")},
        timeout=timeout,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"vdiag {endpoint} failed ({resp.status_code}): {resp.text}")
    result: Dict[str, Any] = resp.json()
    return result


async def aprobe(rating_key: str) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _post, "probe", {"rating_key": rating_key}, _PROBE_TIMEOUT
    )


async def ascan(rating_key: str, duration: Optional[float] = None) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _post, "scan", {"rating_key": rating_key, "duration": duration}, _HEAVY_TIMEOUT
    )


async def aremux(rating_key: str) -> Dict[str, Any]:
    return await asyncio.to_thread(
        _post, "remux", {"rating_key": rating_key}, _HEAVY_TIMEOUT
    )
