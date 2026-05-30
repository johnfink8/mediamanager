"""Client direct-play compatibility heuristics for Plex playback.

Plex decides server-side whether to transcode; we can't perfectly mirror that,
but checking a file's container + codecs (including the H.264/HEVC profile,
level and bit depth — Apple TV's decoder only handles certain ones) against a
client's direct-play profile catches the usual transcode triggers. A file that
must transcode buffers/freezes on a busy server or weak network — a different
problem from on-disk corruption, and worth telling apart.

Consumes the vdiag ``/probe`` summary (container + ``video_streams`` /
``audio_streams``), so it needs no ffmpeg of its own. The per-stream details are
returned alongside the verdict so the model can reason about edge cases the
ruleset doesn't hard-code.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set


@dataclass(frozen=True)
class VideoSpec:
    # ffprobe ``profile`` strings (lowercased); None means any profile is fine.
    profiles: Optional[Set[str]]
    max_level: Optional[float]


@dataclass(frozen=True)
class ClientProfile:
    containers: Set[str]
    video: Dict[str, VideoSpec]
    audio_direct: Set[str]
    # Decoded only via passthrough to a capable receiver, else transcoded.
    audio_passthrough: Set[str]
    max_bit_depth: int


# Apple TV 4K (tvOS Plex app). Values are ffprobe codec_name / profile / format
# tokens. Limits: H.264 High@L4.2 / 1080p, HEVC Main & Main 10 up to L5.1, ≤10-bit.
_APPLETV = ClientProfile(
    containers={"mov", "mp4", "m4v", "matroska", "mpegts", "m2ts"},
    video={
        "h264": VideoSpec(
            profiles={"constrained baseline", "baseline", "main", "high"},
            max_level=4.2,
        ),
        "hevc": VideoSpec(profiles={"main", "main 10"}, max_level=5.1),
        "mpeg4": VideoSpec(profiles=None, max_level=None),
    },
    audio_direct={"aac", "ac3", "eac3", "alac", "mp3", "flac"},
    audio_passthrough={"dca", "dts", "truehd"},
    max_bit_depth=10,
)

_PROFILES: Dict[str, ClientProfile] = {"appletv": _APPLETV}


def _above_1080p(stream: Dict[str, Any]) -> bool:
    return (stream.get("width") or 0) > 1920 or (stream.get("height") or 0) > 1080


def _video_reason(stream: Dict[str, Any], profile: ClientProfile) -> Optional[str]:
    codec = stream.get("codec")
    spec = profile.video.get(codec) if isinstance(codec, str) else None
    if spec is None:
        return f"video codec {codec} needs transcoding"
    prof = (stream.get("profile") or "").lower()
    if spec.profiles is not None and prof and prof not in spec.profiles:
        return f"{codec} profile {stream.get('profile')!r} is not Apple TV direct-play"
    level = stream.get("level_readable")
    if spec.max_level is not None and level and level > spec.max_level:
        return f"{codec} level {level} exceeds Apple TV max {spec.max_level}"
    bit_depth = stream.get("bit_depth")
    if bit_depth and bit_depth > profile.max_bit_depth:
        return f"{bit_depth}-bit video exceeds Apple TV max {profile.max_bit_depth}-bit"
    if codec == "h264" and _above_1080p(stream):
        return "H.264 above 1080p — Apple TV decodes H.264 only up to 1080p"
    return None


def _audio_reason(stream: Dict[str, Any], profile: ClientProfile) -> Optional[str]:
    codec = stream.get("codec")
    if codec in profile.audio_direct:
        return None
    if codec in profile.audio_passthrough:
        return f"{codec} direct-plays only via receiver passthrough, else transcodes"
    return f"audio codec {codec} needs transcoding"


def assess_compatibility(
    probe: Dict[str, Any], client: str = "appletv"
) -> Dict[str, Any]:
    """Assess a probed file against ``client``'s direct-play profile.

    Returns a per-stream breakdown (echoing codec/profile/level/bit-depth) plus
    an overall ``verdict`` of ``direct_play`` or ``transcode`` and the reasons a
    transcode would be triggered.
    """
    profile = _PROFILES.get(client)
    if profile is None:
        raise ValueError(f"unknown client {client!r}; known: {sorted(_PROFILES)}")

    reasons: List[str] = []

    container = probe.get("container")
    container_tokens = set((container or "").split(","))
    container_ok = bool(container_tokens & profile.containers)
    if not container_ok:
        reasons.append(f"container {container!r} is not direct-play")

    video: List[Dict[str, Any]] = []
    for stream in probe.get("video_streams") or []:
        reason = _video_reason(stream, profile)
        if reason:
            reasons.append(reason)
        video.append(
            {
                "codec": stream.get("codec"),
                "profile": stream.get("profile"),
                "level": stream.get("level_readable"),
                "bit_depth": stream.get("bit_depth"),
                "resolution": f"{stream.get('width')}x{stream.get('height')}",
                "ok": reason is None,
                "reason": reason,
            }
        )

    audio: List[Dict[str, Any]] = []
    for stream in probe.get("audio_streams") or []:
        reason = _audio_reason(stream, profile)
        if reason:
            reasons.append(reason)
        audio.append(
            {
                "codec": stream.get("codec"),
                "profile": stream.get("profile"),
                "channels": stream.get("channels"),
                "ok": reason is None,
                "reason": reason,
            }
        )

    return {
        "client": client,
        "verdict": "direct_play" if not reasons else "transcode",
        "container": {
            "name": container,
            "long_name": probe.get("container_long"),
            "ok": container_ok,
        },
        "video": video,
        "audio": audio,
        "transcode_reasons": reasons,
    }
