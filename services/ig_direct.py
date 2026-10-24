"""Direct Instagram client using instagrapi (no Facebook Developer account or Graph token required)."""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.parse
from pathlib import Path
from typing import Any, Callable

from services import db, tokens

log = logging.getLogger(__name__)

KV_DIRECT_SESSION = "ig_direct_session"

# For interactive challenge/2FA code resolution via Telegram
_PENDING_CODE_FUTURE: asyncio.Future[str] | None = None
_MAIN_LOOP: asyncio.AbstractEventLoop | None = None
_ON_CHALLENGE_PROMPT: Callable[[str], Any] | None = None


def is_waiting_for_code() -> bool:
    """Return True if an instagrapi login thread is currently waiting for a 6-digit verification code."""
    return _PENDING_CODE_FUTURE is not None and not _PENDING_CODE_FUTURE.done()


def submit_verification_code(code: str) -> bool:
    """Provide the verification code entered by the user in Telegram."""
    global _PENDING_CODE_FUTURE
    if _PENDING_CODE_FUTURE is not None and not _PENDING_CODE_FUTURE.done() and _MAIN_LOOP is not None:
        clean_code = "".join(ch for ch in code if ch.isdigit())
        _MAIN_LOOP.call_soon_threadsafe(_PENDING_CODE_FUTURE.set_result, clean_code)
        return True
    return False


async def has_direct_session() -> bool:
    """Return True if a direct instagrapi session is stored in the database."""
    record = await db.get_kv(KV_DIRECT_SESSION)
    return bool(record and isinstance(record, dict) and record.get("encrypted_settings"))


async def save_direct_session(settings: dict[str, Any], username: str = "", user_id: str = "") -> None:
    """Encrypt and store instagrapi settings dictionary in KV store."""
    raw_json = json.dumps(settings)
    enc = tokens.encrypt_token(raw_json)
    await db.set_kv(
        KV_DIRECT_SESSION,
        {
            "encrypted_settings": enc,
            "username": username,
            "user_id": str(user_id),
        },
    )
    # Also ensure tokens.get_active_token() returns a marker so scheduler knows auth is configured
    await tokens.save_token("DIRECT_SESSION_ACTIVE", expires_in_sec=365 * 86400)


async def load_direct_settings() -> dict[str, Any] | None:
    """Load and decrypt instagrapi settings from KV store."""
    record = await db.get_kv(KV_DIRECT_SESSION)
    if not record or not isinstance(record, dict):
        return None
    enc = record.get("encrypted_settings")
    if not enc:
        return None
    raw_json = tokens.decrypt_token(enc)
    if not raw_json:
        return None
    try:
        return json.loads(raw_json)
    except Exception as exc:
        log.error("Failed to parse decrypted instagrapi settings: %s", exc)
        return None


def _create_client(settings: dict[str, Any] | None = None):
    from instagrapi import Client

    cl = Client()
    cl.delay_range = [1, 3]
    if settings:
        cl.set_settings(settings)
    return cl


def _challenge_code_handler(username: str, choice: Any) -> str:
    """Called from worker thread by instagrapi when Instagram sends an email/SMS verification code."""
    global _PENDING_CODE_FUTURE
    if _MAIN_LOOP is None:
        raise RuntimeError("No event loop registered for challenge handler")

    future = _MAIN_LOOP.create_future()
    _PENDING_CODE_FUTURE = future

    if _ON_CHALLENGE_PROMPT is not None:
        asyncio.run_coroutine_threadsafe(_ON_CHALLENGE_PROMPT(str(choice)), _MAIN_LOOP)

    # Wait up to 300 seconds (5 minutes) for user to send code in Telegram
    import concurrent.futures

    done_event = concurrent.futures.Future()

    def _on_done(f: asyncio.Future[str]) -> None:
        if not done_event.done():
            if f.cancelled():
                done_event.set_exception(RuntimeError("Cancelled"))
            elif f.exception():
                done_event.set_exception(f.exception())
            else:
                done_event.set_result(f.result())

    _MAIN_LOOP.call_soon_threadsafe(future.add_done_callback, _on_done)
    return done_event.result(timeout=300)


async def login_direct(
    username: str,
    password: str,
    on_challenge_prompt: Callable[[str], Any] | None = None,
    verification_code: str = "",
) -> dict[str, str]:
    """Log in to Instagram using username & password and persist session."""
    global _MAIN_LOOP, _ON_CHALLENGE_PROMPT, _PENDING_CODE_FUTURE
    _MAIN_LOOP = asyncio.get_running_loop()
    _ON_CHALLENGE_PROMPT = on_challenge_prompt
    _PENDING_CODE_FUTURE = None

    existing_settings = await load_direct_settings()

    def _sync_login() -> tuple[dict[str, Any], str, str]:
        cl = _create_client(existing_settings)
        cl.challenge_code_handler = _challenge_code_handler
        try:
            if hasattr(cl, "login_legacy"):
                if verification_code:
                    cl.login_legacy(username, password, verification_code=verification_code)
                else:
                    cl.login_legacy(username, password)
            else:
                if verification_code:
                    cl.login(username, password, verification_code=verification_code)
                else:
                    cl.login(username, password)
        except Exception as first_exc:
            if "field_exception" in str(first_exc) and hasattr(cl, "login_legacy"):
                if verification_code:
                    cl.login_legacy(username, password, verification_code=verification_code)
                else:
                    cl.login_legacy(username, password)
            else:
                raise

        try:
            info = cl.account_info()
            uname = info.username or username
            uid = str(info.pk or cl.user_id or "")
        except Exception:
            uname = str(cl.username or username)
            uid = str(cl.user_id or "")

        return cl.get_settings(), uname, uid

    try:
        settings, uname, uid = await asyncio.to_thread(_sync_login)
        await save_direct_session(settings, username=uname, user_id=uid)
        return {"username": uname, "id": uid}
    finally:
        _PENDING_CODE_FUTURE = None
        _ON_CHALLENGE_PROMPT = None


async def login_by_sessionid(session_id: str) -> dict[str, str]:
    """Log in to Instagram using browser sessionid cookie and persist session."""
    clean_sid = urllib.parse.unquote(session_id.strip().strip('"').strip("'"))

    def _sync_sid_login() -> tuple[dict[str, Any], str, str]:
        cl = _create_client()
        cl.login_by_sessionid(clean_sid)
        info = cl.account_info()
        return cl.get_settings(), info.username, str(info.pk)

    settings, uname, uid = await asyncio.to_thread(_sync_sid_login)
    await save_direct_session(settings, username=uname, user_id=uid)
    return {"username": uname, "id": uid}


async def get_account_info() -> dict[str, str]:
    """Return account info {'id': ..., 'username': ...} using stored direct session."""
    settings = await load_direct_settings()
    if not settings:
        raise ValueError("Instagram direct sessiya topilmadi. /iglogin orqali ulaning.")

    record = await db.get_kv(KV_DIRECT_SESSION) or {}

    def _sync_info() -> tuple[dict[str, Any], str, str]:
        cl = _create_client(settings)
        info = cl.account_info()
        return cl.get_settings(), info.username, str(info.pk)

    try:
        new_settings, uname, uid = await asyncio.to_thread(_sync_info)
        await save_direct_session(new_settings, username=uname, user_id=uid)
        return {"id": uid, "username": uname}
    except Exception as exc:
        # If cached username exists and error is minor, still raise so caller knows
        log.warning("Direct get_account_info error: %s", exc)
        raise


async def upload_reel(video_path: Path, caption: str) -> tuple[str, str]:
    """Upload a video file as an Instagram Reel and return (media_id, permalink)."""
    settings = await load_direct_settings()
    if not settings:
        raise ValueError("Instagram sessiya topilmadi. /iglogin orqali ulaning.")

    def _sync_upload() -> tuple[dict[str, Any], str, str]:
        cl = _create_client(settings)
        media = cl.clip_upload(path=video_path, caption=caption)
        media_id = str(media.pk or media.id)
        code = getattr(media, "code", "")
        permalink = f"https://www.instagram.com/reel/{code}/" if code else f"https://www.instagram.com/p/{code}/"
        return cl.get_settings(), media_id, permalink

    new_settings, media_id, permalink = await asyncio.to_thread(_sync_upload)
    record = await db.get_kv(KV_DIRECT_SESSION) or {}
    await save_direct_session(
        new_settings,
        username=record.get("username", ""),
        user_id=record.get("user_id", ""),
    )
    return media_id, permalink


async def get_media_insights(media_id: str) -> dict[str, int]:
    """Fetch views, likes, and comments for a published Reel."""
    settings = await load_direct_settings()
    if not settings:
        raise ValueError("Instagram sessiya topilmadi.")

    def _sync_insights() -> dict[str, int]:
        cl = _create_client(settings)
        # media_id might be "12345_6789" or "12345"
        pk = int(str(media_id).split("_")[0])
        m = cl.media_info(pk)
        views = getattr(m, "play_count", 0) or getattr(m, "view_count", 0) or 0
        likes = getattr(m, "like_count", 0) or 0
        comments = getattr(m, "comment_count", 0) or 0
        return {
            "views": int(views),
            "plays": int(views),
            "reach": int(views),
            "likes": int(likes),
            "comments": int(comments),
            "saved": 0,
            "shares": 0,
        }

    return await asyncio.to_thread(_sync_insights)


async def check_recent_media_published(caption_prefix: str) -> tuple[str, str] | None:
    """Check recent 5 posts of user to see if caption matching caption_prefix was already published."""
    settings = await load_direct_settings()
    if not settings:
        return None
    record = await db.get_kv(KV_DIRECT_SESSION) or {}
    user_id = record.get("user_id")
    if not user_id:
        return None

    prefix = caption_prefix.strip()[:30]
    if not prefix:
        return None

    def _sync_check() -> tuple[str, str] | None:
        cl = _create_client(settings)
        medias = cl.user_medias(int(user_id), amount=5)
        for m in medias:
            text = (getattr(m, "caption_text", "") or "").strip()
            if prefix in text:
                code = getattr(m, "code", "")
                link = f"https://www.instagram.com/reel/{code}/" if code else ""
                return str(m.pk), link
        return None

    try:
        return await asyncio.to_thread(_sync_check)
    except Exception as exc:
        log.debug("Direct check_recent_media_published failed: %s", exc)
        return None
