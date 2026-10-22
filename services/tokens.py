"""Instagram Access Token storage, Fernet encryption, and automatic refresh."""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any

from cryptography.fernet import Fernet, InvalidToken

import config
from services import db

if TYPE_CHECKING:
    from services.instagram import InstagramClient

log = logging.getLogger(__name__)

_KEY = config.TOKEN_ENC_KEY
try:
    _FERNET = Fernet(_KEY.encode("ascii"))
except Exception:
    # If the key provided in config is invalid, derive a valid key
    import base64
    import hashlib
    derived = hashlib.sha256((config.BOT_TOKEN + ":fernet-salt").encode("utf-8")).digest()
    _FERNET = Fernet(base64.urlsafe_b64encode(derived))


def encrypt_token(plain: str) -> str:
    """Encrypt a plain text token using Fernet."""
    return _FERNET.encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_token(cipher_text: str) -> str:
    """Decrypt a Fernet cipher text into a plain token."""
    return _FERNET.decrypt(cipher_text.encode("ascii")).decode("utf-8")


def redact_token(text: str) -> str:
    """Redact tokens, passwords, and secrets from logs and error strings."""
    if not text:
        return ""
    # Redact access_token parameters
    s = re.sub(r"(access_token|client_secret|TICK_SECRET|token|fb_exchange_token)=([A-Za-z0-9_\-\.\:\~]+)",
               r"\1=[REDACTED]", text, flags=re.I)
    # Redact Authorization: OAuth or Bearer headers
    s = re.sub(r"(Authorization:\s*(?:OAuth|Bearer)\s+)[A-Za-z0-9_\-\.\:\~]+", r"\1[REDACTED]", s, flags=re.I)
    return s


async def get_active_token() -> str | None:
    """Retrieve and decrypt the active Instagram Access Token from the DB or seed env var."""
    item = await db.get_kv("ig_token", None)
    if item is not None and isinstance(item, dict) and "encrypted_token" in item:
        try:
            return decrypt_token(item["encrypted_token"])
        except InvalidToken:
            log.error("failed to decrypt stored Instagram token; check TOKEN_ENC_KEY")
            return None

    # Fallback to seed token from env var if available
    seed = config.IG_ACCESS_TOKEN
    if seed:
        # Standard long-lived tokens last 60 days
        expires_at = time.time() + (60 * 86400)
        await save_token(seed, expires_at=expires_at)
        log.info("seeded initial Instagram token from environment")
        return seed

    return None


async def save_token(plain_token: str, expires_in_sec: float | None = None, expires_at: float | None = None) -> None:
    """Encrypt and persist the token with its expiration timestamp in the DB."""
    if expires_at is None:
        duration = expires_in_sec if expires_in_sec is not None else (60 * 86400)
        expires_at = time.time() + duration

    cipher = encrypt_token(plain_token)
    data = {
        "encrypted_token": cipher,
        "expires_at": expires_at,
        "updated_at": time.time(),
    }
    await db.set_kv("ig_token", data)


async def get_token_status() -> dict[str, Any]:
    """Return dictionary with token state, remaining days, and validity."""
    item = await db.get_kv("ig_token", None)
    if not item or not isinstance(item, dict):
        seed = config.IG_ACCESS_TOKEN
        if seed:
            return {
                "configured": True,
                "days_left": 60,
                "status": "yangi (seed)",
                "expires_at": time.time() + 60 * 86400,
            }
        return {
            "configured": False,
            "days_left": 0,
            "status": "o'rnatilmagan",
            "expires_at": None,
        }

    now = time.time()
    expires_at = item.get("expires_at", now)
    diff_days = max(0, int((expires_at - now) / 86400))
    status_text = "faol" if expires_at > now else "muddati tugagan"
    return {
        "configured": True,
        "days_left": diff_days,
        "status": status_text,
        "expires_at": expires_at,
    }


async def maybe_refresh_token(client: InstagramClient) -> bool:
    """Check remaining token validity; auto-refresh if fewer than 10 days remain."""
    status = await get_token_status()
    if not status["configured"]:
        return False

    days_left = status["days_left"]
    if days_left >= 10:
        return False  # Still healthy

    current_token = await get_active_token()
    if not current_token:
        return False

    log.info("Instagram token has %d days left; attempting refresh", days_left)
    try:
        new_token, expires_in = await client.refresh_access_token(current_token)
        await save_token(new_token, expires_in_sec=expires_in)
        log.info("Instagram token refreshed successfully; valid for %d days", int(expires_in / 86400))
        return True
    except Exception as exc:
        redacted_err = redact_token(str(exc))
        log.error("Instagram token auto-refresh failed: %s", redacted_err)
        return False
