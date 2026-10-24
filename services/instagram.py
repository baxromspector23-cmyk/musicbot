"""Official Meta Graph API client for Instagram Reels publishing and insights."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, AsyncGenerator

import aiohttp

import config
from services.tokens import redact_token

log = logging.getLogger(__name__)


class InstagramError(Exception):
    """Base error for Instagram Graph API issues."""


class InstagramTokenError(InstagramError):
    """Token has expired, been revoked, or is invalid."""


class InstagramRateLimitError(InstagramError):
    """API rate limit or 24-hour content publishing limit reached."""


class InstagramTransientError(InstagramError):
    """Transient network or server error (safe to retry)."""


class InstagramClient:
    """Async client interacting with official Instagram Content Publishing API."""

    def __init__(
        self,
        token: str | None = None,
        ig_user_id: str | None = None,
        base_url: str | None = None,
        upload_base_url: str | None = None,
        api_version: str | None = None,
    ) -> None:
        self.token = token
        self.ig_user_id = ig_user_id or config.IG_USER_ID
        self.api_version = api_version or config.IG_API_VERSION

        if base_url:
            self.base_url = base_url.rstrip("/")
        else:
            host = "graph.instagram.com" if config.IG_LOGIN_MODE == "instagram" else "graph.facebook.com"
            self.base_url = f"https://{host}/{self.api_version}"

        if upload_base_url:
            cleaned = upload_base_url.rstrip("/")
            ver_suffix = f"/{self.api_version}"
            if cleaned.endswith(ver_suffix):
                cleaned = cleaned[: -len(ver_suffix)]
            self.upload_base_url = cleaned
        else:
            self.upload_base_url = "https://rupload.facebook.com/ig-api-upload"

    def _auth_params(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        p = dict(params or {})
        if self.token:
            p["access_token"] = self.token
        return p

    def _check_error(self, data: dict[str, Any], status: int) -> None:
        if "error" in data:
            err = data["error"]
            code = err.get("code")
            subcode = err.get("error_subcode")
            msg = err.get("message", "Unknown Instagram error")

            # Check token errors
            if code in (190, 102) or subcode in (463, 467, 490) or "access blocked" in msg.lower():
                raise InstagramTokenError(f"Instagram token invalid or blocked: {msg}")

            # Check rate / quota limits
            if code in (4, 17, 32, 613) or subcode == 2207042:
                raise InstagramRateLimitError(f"Instagram publishing rate limit reached: {msg}")

            # Transient errors
            if status >= 500 or code in (1, 2):
                raise InstagramTransientError(f"Instagram server error: {msg}")

            raise InstagramError(f"Instagram API error (code {code}, subcode {subcode}): {msg}")

        if status >= 500:
            raise InstagramTransientError(f"HTTP {status} from Instagram server")
        if status != 200:
            raise InstagramError(f"Unexpected HTTP {status} from Instagram: {data}")

    def _is_direct_mode(self) -> bool:
        return self.token == "DIRECT_SESSION_ACTIVE" and "127.0.0.1" not in self.base_url and "localhost" not in self.base_url

    async def get_account_info(self) -> dict[str, Any]:
        """Fetch basic account details (username, id)."""
        if self._is_direct_mode():
            from services import ig_direct
            return await ig_direct.get_account_info()

        url = f"{self.base_url}/{self.ig_user_id}"
        params = self._auth_params({"fields": "id,username"})
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url, params=params) as resp:
                data = await resp.json()
                self._check_error(data, resp.status)
                return data

    async def get_content_publishing_limit(self) -> dict[str, Any]:
        """Fetch content publishing quota usage."""
        if self._is_direct_mode():
            return {"quota_usage": 0, "config": {"quota_total": 50}}

        url = f"{self.base_url}/{self.ig_user_id}/content_publishing_limit"
        params = self._auth_params({"fields": "config,quota_usage"})
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url, params=params) as resp:
                data = await resp.json()
                self._check_error(data, resp.status)
                items = data.get("data", [{}])
                return items[0] if items else {}

    async def get_online_followers(self) -> dict[str, Any] | None:
        """Fetch online followers insight heatmap."""
        if self._is_direct_mode():
            return None

        url = f"{self.base_url}/{self.ig_user_id}/insights"
        params = self._auth_params({"metric": "online_followers", "period": "lifetime"})
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            try:
                async with s.get(url, params=params) as resp:
                    data = await resp.json()
                    if resp.status == 200 and "data" in data and data["data"]:
                        return data["data"][0]
                    return None
            except Exception as exc:
                log.warning("failed to fetch online_followers insight: %s", exc)
                return None

    async def create_reel_container(
        self,
        caption: str,
        file_size: int = 0,
        video_url: str | None = None,
        share_to_feed: bool = True,
    ) -> str:
        """Initialize resumable upload or video_url container."""
        url = f"{self.base_url}/{self.ig_user_id}/media"
        payload: dict[str, Any] = {
            "media_type": "REELS",
            "caption": caption,
            "share_to_feed": "true" if share_to_feed else "false",
        }
        if video_url:
            payload["video_url"] = video_url
        else:
            payload["upload_type"] = "resumable"

        params = self._auth_params(payload)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.post(url, params=params) as resp:
                data = await resp.json()
                self._check_error(data, resp.status)
                container_id = data.get("id")
                if not container_id:
                    raise InstagramError(f"no container id returned: {data}")
                return str(container_id)

    async def upload_video_resumable(self, container_id: str, file_path: Path) -> None:
        """Stream video binary data to rupload.facebook.com with explicit Content-Length."""
        file_size = file_path.stat().st_size
        upload_url = f"{self.upload_base_url}/{self.api_version}/{container_id}"

        headers = {
            "Authorization": f"OAuth {self.token}",
            "offset": "0",
            "file_size": str(file_size),
            "Content-Length": str(file_size),
            "Content-Type": "application/octet-stream",
        }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=600)) as s:
            try:
                with open(file_path, "rb") as fp:
                    async with s.post(upload_url, headers=headers, data=fp) as resp:
                        data = {}
                        try:
                            data = await resp.json()
                        except Exception:
                            pass
                        if resp.status not in (200, 201):
                            self._check_error(data, resp.status)
            except aiohttp.ClientError as exc:
                raise InstagramTransientError(f"Network error during rupload: {exc}") from exc

    async def poll_container_status(self, container_id: str, timeout: float = 600) -> str:
        """Poll container status until FINISHED, ERROR, or timeout."""
        url = f"{self.base_url}/{container_id}"
        params = self._auth_params({"fields": "status_code,status"})
        start_time = asyncio.get_event_loop().time()
        delay = 5.0

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            while True:
                elapsed = asyncio.get_event_loop().time() - start_time
                if elapsed > timeout:
                    raise InstagramTransientError(f"Container {container_id} processing timed out after {timeout}s")

                async with s.get(url, params=params) as resp:
                    data = await resp.json()
                    self._check_error(data, resp.status)
                    code = data.get("status_code", "").upper()

                    if code == "FINISHED":
                        return "FINISHED"
                    if code in ("ERROR", "EXPIRED"):
                        msg = data.get("status", "Media container processing failed")
                        raise InstagramError(f"Container failed with status {code}: {msg}")

                await asyncio.sleep(delay)
                delay = min(15.0, delay * 1.2)

    async def publish_container(self, container_id: str) -> str:
        """Publish the ready media container."""
        url = f"{self.base_url}/{self.ig_user_id}/media_publish"
        params = self._auth_params({"creation_id": container_id})
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.post(url, params=params) as resp:
                data = await resp.json()
                self._check_error(data, resp.status)
                media_id = data.get("id")
                if not media_id:
                    raise InstagramError(f"no media id returned after publish: {data}")
                return str(media_id)

    async def get_media_permalink(self, media_id: str) -> str:
        """Retrieve the public permalink for a published Instagram media item."""
        url = f"{self.base_url}/{media_id}"
        params = self._auth_params({"fields": "id,permalink"})
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            async with s.get(url, params=params) as resp:
                data = await resp.json()
                self._check_error(data, resp.status)
                return data.get("permalink", f"https://www.instagram.com/p/{media_id}/")

    async def check_recent_media_published(self, caption_snippet: str) -> tuple[str, str] | None:
        """Check if an item with a matching caption snippet was recently published (ambiguous-publish recovery)."""
        if not caption_snippet:
            return None
        if self._is_direct_mode():
            from services import ig_direct
            return await ig_direct.check_recent_media_published(caption_snippet)

        url = f"{self.base_url}/{self.ig_user_id}/media"
        params = self._auth_params({"fields": "id,caption,permalink,timestamp", "limit": "10"})
        target = caption_snippet.strip().lower()[:50]

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            try:
                async with s.get(url, params=params) as resp:
                    data = await resp.json()
                    for item in data.get("data", []):
                        cap = str(item.get("caption", "")).strip().lower()
                        if target in cap or (cap and target.startswith(cap[:30])):
                            media_id = str(item["id"])
                            permalink = item.get("permalink", f"https://www.instagram.com/p/{media_id}/")
                            return media_id, permalink
            except Exception as exc:
                log.warning("error checking recent media: %s", exc)
        return None

    async def get_media_insights(self, media_id: str) -> dict[str, int]:
        """Fetch engagement metrics (views, likes, comments, reach, saved)."""
        if self._is_direct_mode():
            from services import ig_direct
            return await ig_direct.get_media_insights(media_id)

        url = f"{self.base_url}/{media_id}/insights"
        params = self._auth_params({"metric": "views,likes,comments,reach,saved"})
        results: dict[str, int] = {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as s:
            try:
                async with s.get(url, params=params) as resp:
                    data = await resp.json()
                    # Fallback to plays if views metric is not supported on older endpoints
                    if "error" in data and "views" in params.get("metric", ""):
                        params_fb = self._auth_params({"metric": "plays,likes,comments,reach,saved"})
                        async with s.get(url, params=params_fb) as resp_fb:
                            data = await resp_fb.json()
                    for entry in data.get("data", []):
                        name = entry.get("name")
                        values = entry.get("values", [{}])
                        val = values[0].get("value", 0) if values else 0
                        if name:
                            results[name] = int(val)
                    if "views" in results and "plays" not in results:
                        results["plays"] = results["views"]
                    elif "plays" in results and "views" not in results:
                        results["views"] = results["plays"]
            except Exception as exc:
                log.warning("could not fetch media insights: %s", exc)
        return results

    async def refresh_access_token(self, token: str) -> tuple[str, float]:
        """Refresh long-lived access token with official Meta endpoint."""
        if token == "DIRECT_SESSION_ACTIVE" and "127.0.0.1" not in self.base_url and "localhost" not in self.base_url:
            return "DIRECT_SESSION_ACTIVE", float(365 * 86400)

        if config.IG_LOGIN_MODE == "instagram":
            if "127.0.0.1" in self.base_url or "localhost" in self.base_url:
                url = f"{self.base_url}/refresh_access_token"
            else:
                url = "https://graph.instagram.com/refresh_access_token"
            params = {
                "grant_type": "ig_refresh_token",
                "access_token": token,
            }
        else:
            # Facebook Login path: exchange long-lived user token
            if "127.0.0.1" in self.base_url or "localhost" in self.base_url:
                url = f"{self.base_url}/oauth/access_token"
            else:
                url = f"https://graph.facebook.com/{self.api_version}/oauth/access_token"
            params = {
                "grant_type": "fb_exchange_token",
                "client_id": config.IG_APP_ID,
                "client_secret": config.IG_APP_SECRET,
                "fb_exchange_token": token,
            }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as s:
            async with s.get(url, params=params) as resp:
                data = await resp.json()
                self._check_error(data, resp.status)
                new_token = data.get("access_token")
                expires_in = float(data.get("expires_in", 60 * 86400))
                if not new_token:
                    raise InstagramError("No new token returned from refresh endpoint")
                return new_token, expires_in
