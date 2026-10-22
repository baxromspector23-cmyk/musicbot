"""In-memory session store and temp-file management."""
from __future__ import annotations

import asyncio
import logging
import random
import shutil
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import config
from services.ffmpeg import Job
from services.probe import MediaInfo
from services.tags import Tags

log = logging.getLogger(__name__)

# Only one FFmpeg job at a time (tiny instance).
JOB_SEM = asyncio.Semaphore(1)


@dataclass
class MergeItem:
    """A file queued for merging."""

    path: Path
    info: MediaInfo
    name: str


@dataclass
class Session:
    """State of the owner's current editing session."""

    user_id: int
    sid: str
    work_dir: Path
    current: Path
    info: MediaInfo
    source_name: str
    previous: Path | None = None
    tags: Tags = field(default_factory=Tags)
    cover: bytes | None = None
    steps: list[str] = field(default_factory=list)
    mode: Literal["idle", "merge"] = "idle"
    merge_files: list[MergeItem] = field(default_factory=list)
    running: Job | None = None
    last_activity: float = field(default_factory=time.monotonic)
    version: int = 0
    prev_state: tuple[MediaInfo, Tags, bytes | None] | None = None


_SESSIONS: dict[int, Session] = {}


def _unlink(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("could not delete %s", path)


def remove_dir(path: Path) -> None:
    """Delete a directory tree; never raises."""
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # pragma: no cover - defensive
        log.warning("rmtree failed for %s", path)


def wipe_root() -> None:
    """Wipe and recreate TEMP_ROOT (startup)."""
    remove_dir(config.TEMP_ROOT)
    config.TEMP_ROOT.mkdir(parents=True, exist_ok=True)


def disk_ok() -> bool:
    """True when enough free disk space remains."""
    config.TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(config.TEMP_ROOT).free >= config.MIN_FREE_DISK_MB * 1024 * 1024


def get(user_id: int) -> Session | None:
    """Return the user's session (touching activity)."""
    s = _SESSIONS.get(user_id)
    if s is not None:
        s.last_activity = time.monotonic()
    return s


def get_valid(user_id: int, sid: str) -> Session | None:
    """Return the session only if the sid matches."""
    s = get(user_id)
    return s if s is not None and s.sid == sid else None


def is_registered(s: Session) -> bool:
    """Whether s is still the active session of its user."""
    return _SESSIONS.get(s.user_id) is s


def allocate(user_id: int) -> tuple[str, Path]:
    """Create a fresh work dir TEMP_ROOT/<user>/<sid>/."""
    sid = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    work = config.TEMP_ROOT / str(user_id) / sid
    work.mkdir(parents=True, exist_ok=True)
    return sid, work


def register(user_id: int, sid: str, work: Path, path: Path, info: MediaInfo, name: str) -> Session:
    """Register a new session for the user."""
    s = Session(user_id=user_id, sid=sid, work_dir=work, current=path, info=info, source_name=name)
    _SESSIONS[user_id] = s
    return s


def end_session(user_id: int) -> None:
    """Kill any running job and delete the user's session files."""
    s = _SESSIONS.pop(user_id, None)
    if s is None:
        return
    try:
        if s.running is not None:
            s.running.cancel()
    finally:
        remove_dir(s.work_dir)


def next_path(s: Session, ext: str) -> Path:
    """Path for the next version file."""
    return s.work_dir / f"v{s.version + 1}.{ext.lstrip('.')}"


def commit(s: Session, path: Path, info: MediaInfo, step: str, tags: Tags, cover: bytes | None) -> None:
    """Make path the current version; keep only current and previous."""
    old_prev = s.previous
    s.prev_state = (s.info, s.tags.copy(), s.cover)
    s.previous = s.current
    s.current = path
    s.info = info
    s.tags = tags
    s.cover = cover
    s.version += 1
    s.steps.append(step)
    _unlink(old_prev)


def undo(s: Session) -> bool:
    """Restore the previous version; False when nothing to undo."""
    if s.previous is None or s.prev_state is None:
        return False
    _unlink(s.current)
    s.current = s.previous
    s.previous = None
    s.info, s.tags, s.cover = s.prev_state
    s.prev_state = None
    if s.steps:
        s.steps.pop()
    return True


def clear_merge(s: Session) -> None:
    """Leave merge mode and delete queued extra files."""
    for item in s.merge_files[1:]:
        _unlink(item.path)
    s.merge_files = []
    s.mode = "idle"


async def cleanup_loop() -> None:
    """Background task: end sessions idle longer than the TTL."""
    while True:
        await asyncio.sleep(config.CLEANUP_INTERVAL_SEC)
        try:
            now = time.monotonic()
            ttl = config.SESSION_TTL_MIN * 60
            for uid, s in list(_SESSIONS.items()):
                if s.running is None and now - s.last_activity > ttl:
                    log.info("session %s expired", s.sid)
                    end_session(uid)
        except Exception:
            log.exception("cleanup failed")
