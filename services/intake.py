"""Download incoming Telegram media and validate it."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import Message

import config
import texts
from services import ffmpeg
from services import session as store
from services import tags as tagsvc
from services.probe import MediaInfo, ProbeError, probe
from utils import UserError

log = logging.getLogger(__name__)

_MIME_EXT = {
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a",
    "audio/aac": "aac", "audio/ogg": "ogg", "audio/opus": "opus", "audio/flac": "flac",
    "audio/x-flac": "flac", "audio/wav": "wav", "audio/x-wav": "wav", "audio/vnd.wave": "wav",
    "audio/x-ms-wma": "wma", "video/mp4": "mp4", "video/quicktime": "mov",
    "video/x-matroska": "mkv", "video/webm": "webm", "video/x-msvideo": "avi",
}


@dataclass
class Incoming:
    """Description of a file attached to a message."""

    file_id: str
    size: int | None
    stem: str
    ext: str


def _split(file_name: str | None, default_stem: str) -> tuple[str, str]:
    if file_name:
        p = Path(file_name)
        return (p.stem or default_stem), p.suffix.lower().lstrip(".")
    return default_stem, ""


def extract_incoming(message: Message) -> Incoming | None:
    """Return file info for supported media messages, else None."""
    if message.audio:
        a = message.audio
        stem, ext = _split(a.file_name, a.title or "audio")
        ext = ext if ext in config.ALLOWED_EXT else _MIME_EXT.get(a.mime_type or "", "mp3")
        return Incoming(a.file_id, a.file_size, stem, ext)
    if message.voice:
        v = message.voice
        return Incoming(v.file_id, v.file_size, "voice", "ogg")
    if message.video:
        v = message.video
        stem, ext = _split(v.file_name, "video")
        return Incoming(v.file_id, v.file_size, stem, ext if ext in config.ALLOWED_EXT else "mp4")
    if message.video_note:
        v = message.video_note
        return Incoming(v.file_id, v.file_size, "video_note", "mp4")
    if message.document:
        d = message.document
        stem, ext = _split(d.file_name, "file")
        mime = d.mime_type or ""
        if ext in config.ALLOWED_EXT:
            return Incoming(d.file_id, d.file_size, stem, ext)
        if mime.startswith(("audio/", "video/")):
            return Incoming(d.file_id, d.file_size, stem, _MIME_EXT.get(mime, "mp3" if mime.startswith("audio/") else "mp4"))
    return None


def precheck_size(inc: Incoming) -> None:
    """Raise UserError if the file is known to be too big."""
    if inc.size is not None and inc.size > config.MAX_DOWNLOAD_MB * 1024 * 1024:
        raise UserError(texts.FILE_TOO_BIG)


async def fetch(bot: Bot, inc: Incoming, dest: Path, require_audio: bool = True) -> MediaInfo:
    """Download to dest, probe and validate; raises UserError on any problem."""
    precheck_size(inc)
    if not store.disk_ok():
        raise UserError(texts.DISK_LOW)
    try:
        await bot.download(inc.file_id, destination=dest)
    except TelegramBadRequest as exc:
        if "too big" in str(exc).lower():
            raise UserError(texts.FILE_TOO_BIG) from exc
        raise
    if dest.stat().st_size > config.MAX_DOWNLOAD_MB * 1024 * 1024:
        raise UserError(texts.FILE_TOO_BIG)
    try:
        info = await probe(dest)
    except ProbeError as exc:
        raise UserError(texts.UNSUPPORTED) from exc
    if require_audio and not info.has_audio:
        raise UserError(texts.NO_AUDIO)
    if not require_audio and not info.has_audio and not info.has_video:
        raise UserError(texts.UNSUPPORTED)
    if info.duration <= 0:
        raise UserError(texts.UNSUPPORTED)
    if info.duration > config.MAX_DURATION_MIN * 60:
        raise UserError(texts.TOO_LONG.format(minutes=config.MAX_DURATION_MIN))
    return info


async def load_cover(work: Path, path: Path, info: MediaInfo) -> bytes | None:
    """Get embedded cover bytes via mutagen, falling back to ffmpeg."""
    if not info.has_cover:
        return None
    data = await asyncio.to_thread(tagsvc.read_cover, path)
    if data:
        return data
    out = work / "cover_src.jpg"
    try:
        await ffmpeg.extract_cover(path, out)
        return await asyncio.to_thread(out.read_bytes)
    except Exception:
        log.warning("cover extraction failed")
        return None
    finally:
        out.unlink(missing_ok=True)
