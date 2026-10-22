"""Application configuration loaded from environment variables."""
from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _fail(message: str) -> None:
    sys.stderr.write(f"CONFIG ERROR: {message}\n")
    raise SystemExit(1)


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        _fail(f"{name} must be an integer, got {raw!r}")
        return default


BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    _fail("BOT_TOKEN is missing. Get it from @BotFather and set it in the environment.")

_owner_raw = os.getenv("OWNER_ID", "0").strip()
try:
    OWNER_ID: int = int(_owner_raw) if _owner_raw else 0
except ValueError:
    OWNER_ID = 0

WEBHOOK_URL: str = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")
RENDER_EXTERNAL_URL: str = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
PORT: int = _get_int("PORT", 10000)
TEMP_ROOT: Path = Path(os.getenv("TEMP_ROOT", "/tmp/musicbot"))
MAX_DOWNLOAD_MB: int = _get_int("MAX_DOWNLOAD_MB", 20)
MAX_UPLOAD_MB: int = _get_int("MAX_UPLOAD_MB", 50)
SESSION_TTL_MIN: int = _get_int("SESSION_TTL_MIN", 30)
FFMPEG_TIMEOUT_SEC: int = _get_int("FFMPEG_TIMEOUT_SEC", 600)
MAX_DURATION_MIN: int = _get_int("MAX_DURATION_MIN", 60)
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"


def _normalize_secret(raw: str) -> str:
    """Return a Telegram-valid secret_token (only A-Za-z0-9_-, 1-256 chars)."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "", raw)[:256]
    if not cleaned:
        cleaned = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()
    return cleaned


WEBHOOK_SECRET: str = _normalize_secret(os.getenv("WEBHOOK_SECRET", ""))
BASE_URL: str = WEBHOOK_URL or RENDER_EXTERNAL_URL
WEBHOOK_PATH = "/webhook"

# ---- constants ----
MAX_MERGE_FILES = 10
MIN_FREE_DISK_MB = 300
MIN_CLIP_SEC = 0.5
END_TOLERANCE_SEC = 1.0
PROGRESS_INTERVAL_SEC = 4.0
CLEANUP_INTERVAL_SEC = 300
CROSSFADE_SEC = 3
NORMALIZE_SINGLE_PASS_SEC = 8 * 60
MAX_TEXT_LEN = 128
THUMB_MAX_PX = 320
THUMB_MAX_KB = 200
COVER_PX = 1000
DEFAULT_BITRATE_K = 192
MIN_BITRATE_K = 128
MAX_BITRATE_K = 320
OPUS_BITRATE_K = 64

ALLOWED_EXT = frozenset(
    {"mp3", "m4a", "aac", "wav", "ogg", "oga", "opus", "flac", "wma",
     "mp4", "mov", "mkv", "webm", "avi"}
)
IMAGE_EXT = frozenset({"jpg", "jpeg", "png", "webp", "bmp"})
