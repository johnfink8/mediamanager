"""vdiag — a tiny ffmpeg sidecar for the mediamanager video tools.

The app containers have no media mount and no ffmpeg; only this service mounts
``/store`` + ``/mnt`` and carries ffmpeg/ffprobe. It exposes three operations
the app calls over the internal docker network:

- ``/probe``  — fast ffprobe header/stream check (seconds)
- ``/scan``   — full ffmpeg decode pass to surface frame corruption (minutes)
- ``/remux``  — lossless container rebuild in place, atomically replacing the file

The wire interface accepts only a **Plex ratingKey**, never a filesystem path —
vdiag resolves the path from Plex itself, so nothing on the network can ask it to
touch an arbitrary file. The resolved path is still constrained to
``VDIAG_ALLOWED_ROOTS`` (realpath-resolved, so symlink escapes are rejected) as a
second gate. Every request must carry ``X-Vdiag-Token`` matching ``VDIAG_TOKEN``.
The service is internal-only — never published to the host or proxied by nginx.
"""

import json
import os
import subprocess
import tempfile
from typing import Any, Dict, List, Optional

import requests
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel

TOKEN: str = os.environ.get("VDIAG_TOKEN", "")
ALLOWED_ROOTS: List[str] = [
    root.rstrip("/")
    for root in os.environ.get("VDIAG_ALLOWED_ROOTS", "/store,/mnt").split(",")
    if root.strip()
]
PLEX_URL: str = os.environ.get("PLEX_URL", "")
PLEX_TOKEN: str = os.environ.get("PLEX_TOKEN", "")
# Containers whose muxer accepts +faststart (an mp4-family option).
_FASTSTART_EXTS = {".mp4", ".m4v", ".mov"}

app = FastAPI(title="vdiag")


class RatingKeyRequest(BaseModel):
    rating_key: str


class ScanRequest(BaseModel):
    rating_key: str
    duration: Optional[float] = None


def _require_token(token: Optional[str]) -> None:
    if not TOKEN:
        raise HTTPException(status_code=500, detail="VDIAG_TOKEN is not configured")
    if token != TOKEN:
        raise HTTPException(status_code=401, detail="invalid vdiag token")


def plex_file_path(rating_key: str) -> str:
    """Resolve a Plex ratingKey to the on-disk file vdiag should operate on.

    This is the only way in: callers name a Plex item, not a path, so they can't
    point vdiag at arbitrary files. Returns the first ``Media[].Part[].file``.
    """
    if not PLEX_URL or not PLEX_TOKEN:
        raise HTTPException(status_code=500, detail="Plex is not configured for vdiag")
    resp = requests.get(
        f"{PLEX_URL}/library/metadata/{rating_key}",
        headers={"X-Plex-Token": PLEX_TOKEN, "Accept": "application/json"},
        timeout=30,
    )
    if resp.status_code >= 400:
        raise HTTPException(
            status_code=502, detail=f"Plex lookup failed ({resp.status_code})"
        )
    items = (resp.json().get("MediaContainer") or {}).get("Metadata") or []
    if not items:
        raise HTTPException(status_code=404, detail=f"no Plex item for {rating_key}")
    for media in items[0].get("Media") or []:
        for part in media.get("Part") or []:
            if part.get("file"):
                file_path: str = part["file"]
                return file_path
    raise HTTPException(status_code=404, detail=f"Plex item {rating_key} has no file")


def safe_path(path: str, must_exist: bool = True) -> str:
    """Resolve ``path`` and confirm it lives under an allowed root.

    Uses realpath so a symlink pointing outside the roots can't smuggle access
    to an arbitrary file.
    """
    real = os.path.realpath(path)
    inside = any(real == root or real.startswith(root + "/") for root in ALLOWED_ROOTS)
    if not inside:
        raise HTTPException(
            status_code=400, detail=f"path outside allowed roots: {path}"
        )
    if must_exist and not os.path.isfile(real):
        raise HTTPException(status_code=404, detail=f"not a file: {path}")
    return real


def _ffprobe(path: str) -> Dict[str, Any]:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ],
        capture_output=True,
        text=True,
    )
    data: Dict[str, Any] = {}
    if proc.stdout.strip():
        data = json.loads(proc.stdout)
    warnings = [line for line in proc.stderr.splitlines() if line.strip()]
    return {"data": data, "warnings": warnings}


def _float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _readable_level(codec: Optional[str], level: Any) -> Optional[float]:
    """ffprobe's raw ``level`` to a human profile level.

    H.264 encodes level as ×10 (42 → 4.2); HEVC as the general_level_idc, ×30
    (153 → 5.1). ffprobe reports -99 when unknown.
    """
    raw = _int(level)
    if raw is None or raw < 0:
        return None
    if codec == "h264":
        return round(raw / 10, 1)
    if codec == "hevc":
        return round(raw / 30, 1)
    return None


def _bit_depth(pix_fmt: Optional[str], bits_per_raw_sample: Any) -> Optional[int]:
    bits = _int(bits_per_raw_sample)
    if bits:
        return bits
    if not pix_fmt:
        return None
    if "12" in pix_fmt:
        return 12
    if "10" in pix_fmt:
        return 10
    return 8


def _summarize(probe: Dict[str, Any]) -> Dict[str, Any]:
    data = probe.get("data") or {}
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]

    duration = _float(fmt.get("duration"))
    size = _float(fmt.get("size"))
    summary: Dict[str, Any] = {
        "container": fmt.get("format_name"),
        "container_long": fmt.get("format_long_name"),
        "duration_sec": duration,
        "bit_rate": fmt.get("bit_rate"),
        "size_bytes": int(size) if size is not None else None,
        "video_streams": [
            {
                "codec": s.get("codec_name"),
                "profile": s.get("profile"),
                "level": s.get("level"),
                "level_readable": _readable_level(s.get("codec_name"), s.get("level")),
                "width": s.get("width"),
                "height": s.get("height"),
                "pix_fmt": s.get("pix_fmt"),
                "bit_depth": _bit_depth(s.get("pix_fmt"), s.get("bits_per_raw_sample")),
                "frame_rate": s.get("avg_frame_rate"),
                "color_transfer": s.get("color_transfer"),
            }
            for s in video
        ],
        "audio_streams": [
            {
                "codec": s.get("codec_name"),
                "profile": s.get("profile"),
                "channels": s.get("channels"),
                "channel_layout": s.get("channel_layout"),
                "sample_rate": s.get("sample_rate"),
            }
            for s in audio
        ],
        "warnings": probe.get("warnings") or [],
    }
    summary["signals"] = {
        "no_video_stream": not video,
        "no_duration": duration is None or duration <= 0,
        "probe_warnings": bool(summary["warnings"]),
        "empty_file": size == 0,
    }
    return summary


@app.get("/health")
def health() -> Dict[str, Any]:
    return {"ok": True, "allowed_roots": ALLOWED_ROOTS}


@app.post("/probe")
def probe(
    req: RatingKeyRequest, x_vdiag_token: Optional[str] = Header(default=None)
) -> Dict[str, Any]:
    _require_token(x_vdiag_token)
    path = safe_path(plex_file_path(req.rating_key))
    return {"rating_key": req.rating_key, "path": path, **_summarize(_ffprobe(path))}


@app.post("/scan")
def scan(
    req: ScanRequest, x_vdiag_token: Optional[str] = Header(default=None)
) -> Dict[str, Any]:
    _require_token(x_vdiag_token)
    path = safe_path(plex_file_path(req.rating_key))
    cmd = ["ffmpeg", "-v", "error"]
    if req.duration is not None:
        cmd += ["-t", str(req.duration)]
    cmd += ["-i", path, "-map", "0:v?", "-f", "null", "-"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    errors = [line for line in proc.stderr.splitlines() if line.strip()]
    return {
        "rating_key": req.rating_key,
        "path": path,
        "ok": not errors,
        "error_count": len(errors),
        "sample_messages": errors[:20],
        "sampled_seconds": req.duration,
    }


@app.post("/remux")
def remux(
    req: RatingKeyRequest, x_vdiag_token: Optional[str] = Header(default=None)
) -> Dict[str, Any]:
    _require_token(x_vdiag_token)
    path = safe_path(plex_file_path(req.rating_key))
    before = _ffprobe(path)

    directory = os.path.dirname(path)
    ext = os.path.splitext(path)[1]
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".vdiag-remux-", suffix=ext)
    os.close(fd)
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", path, "-map", "0", "-c", "copy"]
    if ext.lower() in _FASTSTART_EXTS:
        cmd += ["-movflags", "+faststart"]
    cmd.append(tmp)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            raise HTTPException(
                status_code=422, detail=f"remux failed: {proc.stderr[-1000:]}"
            )
        after = _ffprobe(tmp)
        if not (after.get("data") or {}).get("streams"):
            raise HTTPException(
                status_code=422, detail="remux produced an unreadable file"
            )
        # Same filesystem (same dir) → atomic; keeps the original filename so
        # Plex/Radarr/Sonarr keep tracking the item.
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    return {
        "rating_key": req.rating_key,
        "path": path,
        "remuxed": True,
        "before": _summarize(before),
        "after": _summarize(_ffprobe(path)),
    }
