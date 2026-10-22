"""Sending result files to the owner."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import FSInputFile

import config
import texts
from services import ffmpeg
from services.probe import MediaInfo
from services.session import Session
from utils import fmt_size, sanitize_filename

log = logging.getLogger(__name__)


def result_filename(s: Session, ext: str) -> str:
    """'<artist> - <title>.<ext>' sanitized, fallback '<stem>_edit.<ext>'."""
    t = s.tags
    base = ""
    if t.title:
        base = f"{t.artist} - {t.title}" if t.artist else t.title
    base = sanitize_filename(base) or sanitize_filename(f"{s.source_name}_edit") or "audio_edit"
    return f"{base}.{ext.lstrip('.')}"


async def _with_retry(factory):  # type: ignore[no-untyped-def]
    try:
        return await factory()
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after + 1)
        return await factory()


async def send_result(bot: Bot, chat_id: int, s: Session, path: Path, info: MediaInfo) -> bool:
    """Send the file as audio (mp3/m4a) or document; False when too large."""
    size = path.stat().st_size
    if size > config.MAX_UPLOAD_MB * 1024 * 1024:
        await bot.send_message(chat_id, texts.RESULT_TOO_BIG.format(size=fmt_size(size), limit=config.MAX_UPLOAD_MB))
        return False
    ext = path.suffix.lower().lstrip(".")
    name = result_filename(s, ext)
    file = FSInputFile(path, filename=name)
    if ext in ("mp3", "m4a"):
        thumb: FSInputFile | None = None
        if s.cover:
            cover_path = s.work_dir / "cover.jpg"
            thumb_path = s.work_dir / "thumb.jpg"
            await asyncio.to_thread(cover_path.write_bytes, s.cover)
            if await ffmpeg.make_thumbnail(cover_path, thumb_path):
                thumb = FSInputFile(thumb_path)
        await _with_retry(lambda: bot.send_audio(
            chat_id, file, title=s.tags.title or None, performer=s.tags.artist or None,
            duration=int(info.duration), thumbnail=thumb,
        ))
    else:
        await _with_retry(lambda: bot.send_document(chat_id, file))
    return True
