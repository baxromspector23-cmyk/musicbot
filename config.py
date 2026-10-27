"""Application configuration loaded from environment variables."""
from __future__ import annotations

import base64
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
_render_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
if not _render_url and os.getenv("RENDER_EXTERNAL_HOSTNAME", "").strip():
    _render_url = f"https://{os.getenv('RENDER_EXTERNAL_HOSTNAME', '').strip()}"
RENDER_EXTERNAL_URL: str = _render_url
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

# ======================================================================
# Instagram Reels Auto-Poster Configuration
# ======================================================================

_DEFAULT_DB_FILE = (Path(__file__).resolve().parent / "data.db").as_posix()
DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{_DEFAULT_DB_FILE}").strip()
# Normalize postgres URL scheme if provided as postgres:// or postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://") and not DATABASE_URL.startswith("postgresql+"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)


def _get_encryption_key() -> str:
    key = os.getenv("TOKEN_ENC_KEY", "").strip()
    if key:
        return key
    # Derive a stable 32-byte urlsafe-base64 key from BOT_TOKEN if not explicitly set
    derived = hashlib.sha256((BOT_TOKEN + ":fernet-salt").encode("utf-8")).digest()
    return base64.urlsafe_b64encode(derived).decode("ascii")


TOKEN_ENC_KEY: str = _get_encryption_key()

IG_LOGIN_MODE: str = os.getenv("IG_LOGIN_MODE", "instagram").strip().lower()  # "instagram" or "facebook"
IG_USER_ID: str = os.getenv("IG_USER_ID", "").strip()
IG_ACCESS_TOKEN: str = os.getenv("IG_ACCESS_TOKEN", "").strip()
IG_APP_ID: str = os.getenv("IG_APP_ID", "").strip()
IG_APP_SECRET: str = os.getenv("IG_APP_SECRET", "").strip()
IG_API_VERSION: str = os.getenv("IG_API_VERSION", "v25.0").strip()

LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "gemini").strip().lower()  # "gemini" or "anthropic"
LLM_MODEL: str = os.getenv("LLM_MODEL", "").strip()
if not LLM_MODEL:
    LLM_MODEL = "gemini-pro-latest" if LLM_PROVIDER == "gemini" else "claude-3-5-haiku-20241022"

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "").strip()
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "").strip()
CAPTION_LANG: str = os.getenv("CAPTION_LANG", "uz").strip()
BRAND_HASHTAG: str = os.getenv("BRAND_HASHTAG", "").strip().lstrip("#")
DEFAULT_HASHTAGS: list[str] = [
    t.strip().lstrip("#")
    for t in os.getenv("DEFAULT_HASHTAGS", "saytyaratish,biznesuz,tadbirkor,telegrambot,restoranuz,vebsayt").split(",")
    if t.strip()
]
HASHTAG_LIMIT: int = _get_int("HASHTAG_LIMIT", 5)

TZ: str = os.getenv("TZ", "Asia/Tashkent").strip()
MAX_POSTS_PER_DAY: int = _get_int("MAX_POSTS_PER_DAY", 4)
MIN_GAP_HOURS: int = _get_int("MIN_GAP_HOURS", 2)
QUIET_HOURS: str = os.getenv("QUIET_HOURS", "0-7").strip()
AUTOPILOT_SLOTS: list[str] = [
    s.strip()
    for s in os.getenv("AUTOPILOT_SLOTS", "09:00,11:30,18:30,20:30").split(",")
    if s.strip()
]

_storage_chat_raw = os.getenv("STORAGE_CHAT_ID", "").strip()
STORAGE_CHAT_ID: int | None = None
if _storage_chat_raw:
    try:
        STORAGE_CHAT_ID = int(_storage_chat_raw)
    except ValueError:
        pass

_tick_raw = os.getenv("TICK_SECRET", "").strip()
if not _tick_raw:
    _tick_raw = hashlib.sha256((BOT_TOKEN + ":tick-secret").encode("utf-8")).hexdigest()[:32]
TICK_SECRET: str = _tick_raw

SCHEDULER_INTERVAL_SEC: int = _get_int("SCHEDULER_INTERVAL_SEC", 30)
MAX_REEL_MB: int = _get_int("MAX_REEL_MB", 19)
