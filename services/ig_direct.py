"""Direct Instagram client using instagrapi (no Facebook Developer account or Graph token required)."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
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
    """Load and decrypt instagrapi settings from KV store, falling back to IG_SESSION_B64 env var."""
    record = await db.get_kv(KV_DIRECT_SESSION)
    if record and isinstance(record, dict) and record.get("encrypted_settings"):
        raw_json = tokens.decrypt_token(record["encrypted_settings"])
        if raw_json:
            try:
                return json.loads(raw_json)
            except Exception as exc:
                log.error("Failed to parse decrypted instagrapi settings: %s", exc)

    env_b64 = os.getenv("IG_SESSION_B64", "").strip()
    if env_b64:
        try:
            decoded = base64.b64decode(env_b64).decode("utf-8")
            settings = json.loads(decoded)
            if isinstance(settings, dict):
                await save_direct_session(settings)
                log.info("Seeded Instagram direct session from IG_SESSION_B64 environment variable")
                return settings
        except Exception as exc:
            log.error("Failed to decode IG_SESSION_B64: %s", exc)
    return None


def _create_client(settings: dict[str, Any] | None = None):
    from instagrapi import Client

    cl = Client()
    cl.delay_range = [1, 3]
    if settings:
        cl.set_settings(settings)
    # Bypass broken IGUSDIDRegistrationMutation on graphql_www which returns field_exception
    try:
        if not getattr(cl, "usdid_private_key", None):
            cl.usdid_generate()
        cl.usdid_registered = True
    except Exception:
        pass
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
        cl.username = username
        cl.password = password

        outcome = cl.bloks_caa_login(
            username=username,
            password=password,
            verification_code=verification_code,
        )
        logged = bool(outcome.get("logged_in"))
        if not logged:
            raw_text = cl._bloks_all_text(outcome.get("result", {}))
            if "Incorrect password" in raw_text or "login_wrong_password" in raw_text:
                raise ValueError("Instagram paroli noto'g'ri! Iltimos, parolni tekshirib qayta urinib ko'ring.")
            if "user_not_found" in raw_text.lower() or "account_not_found" in raw_text.lower():
                raise ValueError(f"@{username} nomli Instagram akkaunt topilmadi.")
            context = cl._extract_two_step_verification_context(outcome)
            if context:
                if not verification_code:
                    code = _challenge_code_handler(username, "2FA / Authenticator")
                else:
                    code = verification_code
                logged = cl._login_with_bloks_two_factor(code, outcome, RuntimeError("2FA required"))
            if not logged:
                reason = str(outcome.get("reason") or "Instagram login rad etildi (ilovada tasdiqlang)")
                raise ValueError(reason)

        try:
            cl.login_flow()
        except Exception as exc:
            log.debug("post-login flow warning: %s", exc)

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

    thumb_path = video_path.with_suffix(".jpg")
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "0.5",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(thumb_path),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if not thumb_path.exists() or thumb_path.stat().st_size == 0:
            # Fallback to frame at 0.0s
            proc2 = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(video_path),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(thumb_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc2.communicate(), timeout=30)
    except Exception as exc:
        log.warning("Thumbnail extraction via ffmpeg failed: %s", exc)

    def _sync_upload() -> tuple[dict[str, Any], str, str]:
        try:
            import moviepy
            if not hasattr(moviepy, "VideoFileClip"):
                from moviepy.editor import VideoFileClip
                moviepy.VideoFileClip = VideoFileClip  # type: ignore[attr-defined]
        except Exception:
            pass
        cl = _create_client(settings)
        kwargs: dict[str, Any] = {"path": video_path, "caption": caption}
        if thumb_path.exists() and thumb_path.stat().st_size > 0:
            kwargs["thumbnail"] = thumb_path
        media = cl.clip_upload(**kwargs)
        media_id = str(media.pk or media.id)
        code = getattr(media, "code", "")
        permalink = f"https://www.instagram.com/reel/{code}/" if code else f"https://www.instagram.com/p/{code}/"
        return cl.get_settings(), media_id, permalink

    try:
        new_settings, media_id, permalink = await asyncio.to_thread(_sync_upload)
    finally:
        thumb_path.unlink(missing_ok=True)
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
