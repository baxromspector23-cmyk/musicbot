"""ffprobe wrapper producing MediaInfo."""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path


class ProbeError(Exception):
    """ffprobe failed or the file is not media."""


@dataclass
class MediaInfo:
    """Facts about a media file."""

    duration: float = 0.0
    codec_name: str = ""
    sample_rate: int = 44100
    channels: int = 2
    bit_rate: int = 0
    ext: str = ""
    has_video: bool = False
    has_audio: bool = True
    title: str = ""
    artist: str = ""
    album: str = ""
    has_cover: bool = False
    size: int = 0

    @property
    def native_ext(self) -> str:
        """Native extension corresponding to this audio codec."""
        c = self.codec_name
        if c == "mp3":
            return "mp3"
        if c == "aac":
            return "m4a"
        if c == "flac":
            return "flac"
        if c in ("opus", "vorbis"):
            return "ogg"
        if c.startswith("pcm_"):
            return "wav"
        return "mp3"

    @property
    def out_ext(self) -> str:
        """Always produce mp3 when editing so Telegram delivers it as a playable music track."""
        return "mp3"


def _to_int(value: object) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _to_float(value: object) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


async def probe(path: Path) -> MediaInfo:
    """Run ffprobe (JSON output) and build MediaInfo."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), 60)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise ProbeError("ffprobe timeout") from exc
    if proc.returncode != 0:
        raise ProbeError("ffprobe failed")
    try:
        data = json.loads(out.decode("utf-8", "ignore"))
    except json.JSONDecodeError as exc:
        raise ProbeError("bad ffprobe output") from exc

    streams = data.get("streams", [])
    fmt = data.get("format", {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    info = MediaInfo(ext=path.suffix.lower().lstrip("."), size=_to_int(fmt.get("size")))
    video_stream = None
    for s in streams:
        if s.get("codec_type") == "video":
            if s.get("disposition", {}).get("attached_pic") == 1:
                info.has_cover = True
            else:
                info.has_video = True
                if video_stream is None:
                    video_stream = s
    if audio is None:
        info.has_audio = False
        info.duration = _to_float(fmt.get("duration")) or (
            _to_float(video_stream.get("duration")) if video_stream else 0.0
        )
        return info
    info.codec_name = str(audio.get("codec_name", ""))
    info.sample_rate = _to_int(audio.get("sample_rate")) or 44100
    info.channels = _to_int(audio.get("channels")) or 2
    info.bit_rate = _to_int(audio.get("bit_rate")) or _to_int(fmt.get("bit_rate"))
    info.duration = _to_float(fmt.get("duration")) or _to_float(audio.get("duration"))
    tags: dict[str, str] = {}
    for src in (audio.get("tags") or {}, fmt.get("tags") or {}):
        for k, v in src.items():
            tags.setdefault(str(k).lower(), str(v))
    info.title = tags.get("title", "")
    info.artist = tags.get("artist", "")
    info.album = tags.get("album", "")
    return info
