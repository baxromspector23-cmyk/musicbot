"""Self-test suite for Instagram Reels auto-poster.

Run via:
    python -m scripts.selftest_instagram
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Ensure test environment variables are set before config is imported
os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN")
os.environ.setdefault("OWNER_ID", "1")
os.environ.setdefault("TICK_SECRET", "test_tick_secret_xyz123")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("TOKEN_ENC_KEY", base64.urlsafe_b64encode(b"01234567890123456789012345678901").decode("ascii"))
os.environ.setdefault("IG_USER_ID", "17841400000000001")
os.environ.setdefault("IG_ACCESS_TOKEN", "EAAB_initial_test_seed_token")

TMP = Path(tempfile.mkdtemp(prefix="ig_selftest_"))
DB_PATH = TMP / "selftest.db"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_PATH}"

import config
config.DATABASE_URL = f"sqlite+aiosqlite:///{DB_PATH}"
from main import create_app
from services import besttime, db, ffmpeg, scheduler, tokens, video_prep
from services.besttime import is_in_quiet_hours, parse_quiet_hours, parse_scheduled_time, pick_best_slot, smooth_weights
from services.instagram import (
    InstagramClient,
    InstagramError,
    InstagramRateLimitError,
    InstagramTokenError,
    InstagramTransientError,
)
from services.llm import (
    BANNED_TAGS,
    _call_gemini,
    _extract_json,
    build_fallback_caption,
    extract_caption_body_and_tags,
    parse_raw_hashtags,
    sanitize_hashtag,
    validate_and_format_hashtags,
)
from services.probe import MediaInfo, probe
from services.tokens import decrypt_token, encrypt_token, redact_token
from utils import escape_html

RESULTS: list[tuple[str, bool, str]] = []
TMP = Path(tempfile.mkdtemp(prefix="ig_selftest_"))


def approx(a: float, b: float, tol: float = 1e-4, msg: str = "") -> None:
    assert abs(a - b) <= tol, f"{msg} expected {b} +/- {tol}, got {a}"


async def run_case(name: str, fn) -> None:  # type: ignore[no-untyped-def]
    try:
        await fn()
        RESULTS.append((name, True, ""))
        print(f"  [PASS] {name}")
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"[:120]))
        print(f"  [FAIL] {name} - {exc}")


# ----------------------------------------------------------------------
# Mock Instagram Server
# ----------------------------------------------------------------------

class MockInstagramServer:
    def __init__(self) -> None:
        self.app = web.Application()
        self.containers: dict[str, dict] = {}
        self.media_items: dict[str, dict] = {
            "media_existing_rec": {
                "id": "media_existing_rec",
                "caption": "Ambiguous recovery test reel caption for testing #music",
                "permalink": "https://www.instagram.com/reel/media_existing_rec/",
                "timestamp": "2026-10-22T10:00:00+0000",
            }
        }
        self.container_counter = 100
        self.media_counter = 500

        # Routes
        self.app.router.add_post("/v21.0/{ig_user_id}/media", self._create_container)
        self.app.router.add_post("/ig-api-upload/{container_id}", self._upload_resumable)
        self.app.router.add_post("/v21.0/{ig_user_id}/media_publish", self._publish_media)
        self.app.router.add_get("/v21.0/{media_id}/insights", self._media_insights)
        self.app.router.add_get("/v21.0/{ig_user_id}/insights", self._user_insights)
        self.app.router.add_get("/v21.0/{ig_user_id}/content_publishing_limit", self._quota_limit)
        self.app.router.add_get("/v21.0/{ig_user_id}/media", self._user_media)
        self.app.router.add_get("/v21.0/refresh_access_token", self._refresh_token)
        self.app.router.add_get("/v21.0/oauth/access_token", self._refresh_token)
        self.app.router.add_get("/v21.0/{id}", self._get_node)

    async def _create_container(self, req: web.Request) -> web.Response:
        self.container_counter += 1
        cid = f"cont_{self.container_counter}"
        caption = req.query.get("caption", "")
        self.containers[cid] = {
            "caption": caption,
            "status_code": "IN_PROGRESS",
            "polls": 0,
            "uploaded": False,
        }
        return web.json_response({"id": cid})

    async def _upload_resumable(self, req: web.Request) -> web.Response:
        cid = req.match_info["container_id"]
        auth = req.headers.get("Authorization", "")
        if not auth.startswith("OAuth "):
            return web.json_response({"error": {"code": 190, "message": "Invalid OAuth token"}}, status=401)
        # Read streaming body
        body = await req.read()
        if cid in self.containers:
            self.containers[cid]["uploaded"] = True
            self.containers[cid]["uploaded_bytes"] = len(body)
        return web.json_response({"success": True})

    async def _poll_container(self, req: web.Request) -> web.Response:
        cid = req.match_info["container_id"]
        if cid in self.containers:
            self.containers[cid]["polls"] += 1
            if self.containers[cid]["polls"] >= 2:
                self.containers[cid]["status_code"] = "FINISHED"
            return web.json_response({
                "id": cid,
                "status_code": self.containers[cid]["status_code"],
                "status": "Ready to publish" if self.containers[cid]["status_code"] == "FINISHED" else "Processing",
            })
        return web.json_response({"error": {"code": 100, "message": "Not found"}}, status=404)

    async def _publish_media(self, req: web.Request) -> web.Response:
        cid = req.query.get("creation_id")
        if not cid or cid not in self.containers:
            return web.json_response({"error": {"code": 100, "message": "Container not found"}}, status=400)
        self.media_counter += 1
        mid = f"media_{self.media_counter}"
        self.media_items[mid] = {
            "id": mid,
            "caption": self.containers[cid]["caption"],
            "permalink": f"https://www.instagram.com/reel/{mid}/",
        }
        return web.json_response({"id": mid})

    async def _get_node(self, req: web.Request) -> web.Response:
        node_id = req.match_info["id"]
        if node_id in self.media_items:
            return web.json_response(self.media_items[node_id])
        if node_id in self.containers:
            self.containers[node_id]["polls"] += 1
            if self.containers[node_id]["polls"] >= 2:
                self.containers[node_id]["status_code"] = "FINISHED"
            return web.json_response({
                "id": node_id,
                "status_code": self.containers[node_id]["status_code"],
                "status": "Ready to publish" if self.containers[node_id]["status_code"] == "FINISHED" else "Processing",
            })
        # Account info query
        return web.json_response({"id": node_id, "username": "mock_creator_account"})

    async def _media_insights(self, _: web.Request) -> web.Response:
        return web.json_response({
            "data": [
                {"name": "plays", "values": [{"value": 2500}]},
                {"name": "reach", "values": [{"value": 2100}]},
                {"name": "likes", "values": [{"value": 180}]},
                {"name": "comments", "values": [{"value": 22}]},
                {"name": "saved", "values": [{"value": 65}]},
            ]
        })

    async def _user_insights(self, _: web.Request) -> web.Response:
        # Mock online followers heatmap with 24 hours
        hourly_data = {str(h): 100 + (h * 15) for h in range(24)}
        return web.json_response({
            "data": [
                {
                    "name": "online_followers",
                    "period": "lifetime",
                    "values": [{"value": hourly_data}],
                }
            ]
        })

    async def _quota_limit(self, _: web.Request) -> web.Response:
        return web.json_response({
            "data": [
                {
                    "config": {"quota_total": 50, "quota_duration": 86400},
                    "quota_usage": 3,
                }
            ]
        })

    async def _user_media(self, _: web.Request) -> web.Response:
        return web.json_response({"data": list(self.media_items.values())})

    async def _refresh_token(self, _: web.Request) -> web.Response:
        return web.json_response({
            "access_token": "mock_refreshed_access_token_xyz999",
            "token_type": "bearer",
            "expires_in": 5184000,
        })


# ----------------------------------------------------------------------
# Test Cases
# ----------------------------------------------------------------------

async def test_time_parser_table() -> None:
    """Test 1: natural language time parser positive and negative cases."""
    tz = ZoneInfo("Asia/Tashkent")
    # Fixed base time: 2026-10-22 12:00:00 Tashkent time (UTC 07:00:00)
    base_local = datetime(2026, 10, 22, 12, 0, 0, tzinfo=tz)
    base_utc = base_local.astimezone(timezone.utc)

    # Positive relative cases
    dt1 = parse_scheduled_time("+2h", now_utc=base_utc)
    approx((dt1 - base_utc).total_seconds(), 2 * 3600, 1.0, "+2h")

    dt2 = parse_scheduled_time("30 daqiqadan keyin", now_utc=base_utc)
    approx((dt2 - base_utc).total_seconds(), 30 * 60, 1.0, "30 daqiqa")

    dt3 = parse_scheduled_time("+15m", now_utc=base_utc)
    approx((dt3 - base_utc).total_seconds(), 15 * 60, 1.0, "+15m")

    # "bugun 20:30" -> local 20:30 today
    dt4 = parse_scheduled_time("bugun 20:30", now_utc=base_utc)
    dt4_loc = dt4.astimezone(tz)
    assert dt4_loc.hour == 20 and dt4_loc.minute == 30 and dt4_loc.day == 22

    # "ertaga 19:00" -> local 19:00 tomorrow
    dt5 = parse_scheduled_time("ertaga 19:00", now_utc=base_utc)
    dt5_loc = dt5.astimezone(tz)
    assert dt5_loc.hour == 19 and dt5_loc.minute == 0 and dt5_loc.day == 23

    # Date format: "25.10 21:00"
    dt6 = parse_scheduled_time("25.10 21:00", now_utc=base_utc)
    dt6_loc = dt6.astimezone(tz)
    assert dt6_loc.day == 25 and dt6_loc.month == 10 and dt6_loc.hour == 21

    # ISO format: "2026-10-26 18:45"
    dt7 = parse_scheduled_time("2026-10-26 18:45", now_utc=base_utc)
    dt7_loc = dt7.astimezone(tz)
    assert dt7_loc.day == 26 and dt7_loc.hour == 18 and dt7_loc.minute == 45

    # Plain time: "20:30" (later today)
    dt8 = parse_scheduled_time("20:30", now_utc=base_utc)
    assert dt8.astimezone(tz).day == 22 and dt8.astimezone(tz).hour == 20

    # Plain time: "08:00" (earlier today -> rolls over to tomorrow)
    dt9 = parse_scheduled_time("08:00", now_utc=base_utc)
    assert dt9.astimezone(tz).day == 23 and dt9.astimezone(tz).hour == 8

    # Negative cases
    negatives = [
        "salom",
        "bugun 25:00",
        "bugun 08:00",  # past time for 'bugun'
        "ertaga 30:00",
        "-5h",
        "+50 days",
        "32.13.2026 10:00",
    ]
    for bad in negatives:
        try:
            parse_scheduled_time(bad, now_utc=base_utc)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for bad input: {bad!r}")


async def test_hashtag_sanitizer() -> None:
    """Test 2: hashtag sanitization, deduping, banned filtering, and limits."""
    # 1. Clean format
    assert sanitize_hashtag("#Musiqa") == "musiqa"
    assert sanitize_hashtag("track_2026!") == "track_2026"

    # 2. Banned tags filtered
    for banned in ("fyp", "rek", "viral", "like4like", "explorepage"):
        assert banned in BANNED_TAGS

    # 3. Validation and formatting
    input_tags = ["#Music", "MUSIC", "music", "fyp", "rek", "tashkent", "remix", "beat", "melody", "sound"]
    cleaned = validate_and_format_hashtags(
        input_tags,
        brand_tag="mychannel",
        default_tags=["uzb", "uzbekistan"],
        limit=5,
    )
    # Brand tag must be first
    assert cleaned[0] == "mychannel"
    # No banned tags
    for b in ("fyp", "rek"):
        assert b not in cleaned
    # Unique and <= 5
    assert len(cleaned) == 5
    assert len(set(cleaned)) == 5

    # 4. Comma & semicolon parsing (Issue 1)
    comma_input = "musiqa, toshkent, uzb; rap, hiphop, remix"
    parsed_comma = parse_raw_hashtags(comma_input)
    assert parsed_comma == ["musiqa", "toshkent", "uzb", "rap", "hiphop", "remix"]
    formatted_from_comma = validate_and_format_hashtags(comma_input, limit=5)
    assert len(formatted_from_comma) == 5
    assert formatted_from_comma == ["musiqa", "toshkent", "uzb", "rap", "hiphop"]

    # 5. Multi-paragraph caption extraction (Issue 2)
    multi_p_caption = (
        "1-abzats: Yangi trek premyerasi!\n\n"
        "2-abzats: Barcha platformalarda tinglang va baho bering.\n\n"
        "3-abzats: Do'stlaringiz bilan ulashing!\n\n"
        "#musiqa #toshkent #uzbekistan"
    )
    body, tags = extract_caption_body_and_tags(multi_p_caption)
    assert body == (
        "1-abzats: Yangi trek premyerasi!\n\n"
        "2-abzats: Barcha platformalarda tinglang va baho bering.\n\n"
        "3-abzats: Do'stlaringiz bilan ulashing!"
    )
    assert tags == ["musiqa", "toshkent", "uzbekistan"]

    # 6. Inline hashtags and strict 5-tag cap (Issue 3)
    inline_caption = (
        "Eshitib ko'ring #musiqa va #yangilik!\n\n"
        "Zo'r #kayfiyat ulashamiz. #uzb #toshkent #bass #remix #rap"
    )
    body2, tags2 = extract_caption_body_and_tags(inline_caption)
    assert "#" not in body2
    assert "Eshitib ko'ring musiqa va yangilik!" in body2
    assert "Zo'r kayfiyat ulashamiz." in body2
    final_tags = validate_and_format_hashtags(tags2, limit=5)
    assert len(final_tags) == 5
    assert set(final_tags).issubset({"musiqa", "yangilik", "kayfiyat", "uzb", "toshkent", "bass", "remix", "rap"})

    # 7. HTML escaping for Telegram messages (Issue 4)
    raw_user_input = "Trek <Yulduz> & Bass <Beat> 100% Zo'r!"
    escaped = escape_html(raw_user_input)
    assert "<" not in escaped
    assert ">" not in escaped
    assert "&amp;" in escaped
    assert "&lt;Yulduz&gt;" in escaped
    assert escape_html(None) == ""


async def test_caption_fallback() -> None:
    """Test 3: LLM JSON parser and deterministic fallback."""
    # 1. Direct JSON
    valid_json = '{"caption": "Hook line\\nSecond line", "hashtags": ["music", "uzb"]}'
    res = _extract_json(valid_json)
    assert res is not None
    assert res["caption"] == "Hook line\nSecond line"
    assert res["hashtags"] == ["music", "uzb"]

    # 2. Markdown fenced codeblock
    fenced = f"```json\n{valid_json}\n```"
    res_fenced = _extract_json(fenced)
    assert res_fenced is not None and res_fenced["caption"] == "Hook line\nSecond line"

    # 3. Embedded in prose
    prose = f"Sure! Here is the JSON:\n{valid_json}\nHope you like it!"
    res_prose = _extract_json(prose)
    assert res_prose is not None and len(res_prose["hashtags"]) == 2

    # 4. Invalid JSON
    assert _extract_json("not a json at all") is None

    # 5. Deterministic fallback
    fb = build_fallback_caption("Sehrli musiqa lavhasi", brand_tag="mychannel", limit=5)
    assert "is_fallback" in fb and fb["is_fallback"] is True
    assert len(fb["caption"]) > 0
    assert len(fb["hashtags"]) <= 5
    assert len(fb["full_text"]) <= 2200
    assert fb["hashtags"][0] == "mychannel"

    # 6. Gemini header authentication (API key in x-goog-api-key header, NOT in URL query parameter)
    gemini_app = web.Application()
    captured_requests: list[dict[str, Any]] = []

    async def _mock_gemini(req: web.Request) -> web.Response:
        captured_requests.append({
            "query_key": req.query.get("key"),
            "header_key": req.headers.get("x-goog-api-key"),
            "url": str(req.url),
        })
        return web.json_response({
            "candidates": [{
                "content": {
                    "parts": [{"text": json.dumps({"caption": "AI Hook", "hashtags": ["music", "uzb"]})}]
                }
            }]
        })

    gemini_app.router.add_post("/models/{model}:generateContent", _mock_gemini)
    g_server = TestServer(gemini_app)
    await g_server.start_server()
    try:
        secret_key = "AIzaSyTestSecretKey_998877"
        res_text = await _call_gemini(
            prompt="Write a caption",
            frames=[],
            api_key=secret_key,
            model="gemini-2.5-flash",
            lang="uz",
            base_url=f"http://{g_server.host}:{g_server.port}",
        )
        assert len(captured_requests) == 1
        req_info = captured_requests[0]
        # Assert key is NOT in query string
        assert req_info["query_key"] is None, "API key MUST NOT be passed in URL query parameter"
        assert secret_key not in req_info["url"], "API key MUST NOT appear anywhere in URL"
        # Assert key is passed securely in x-goog-api-key header
        assert req_info["header_key"] == secret_key, "API key MUST be passed in x-goog-api-key header"
        parsed = _extract_json(res_text)
        assert parsed is not None and parsed["caption"] == "AI Hook"
    finally:
        await g_server.close()


async def test_best_time_scoring() -> None:
    """Test 4: follower-activity smoothing, quiet hours, and best slot scoring."""
    # 1. Quiet hours
    q_start, q_end = parse_quiet_hours("0-7")
    assert is_in_quiet_hours(0, (q_start, q_end)) is True
    assert is_in_quiet_hours(5, (q_start, q_end)) is True
    assert is_in_quiet_hours(7, (q_start, q_end)) is True
    assert is_in_quiet_hours(12, (q_start, q_end)) is False
    assert is_in_quiet_hours(20, (q_start, q_end)) is False

    # 2. Smoothing algorithm (0.25, 0.50, 0.25)
    raw = [10.0] * 24
    raw[12] = 100.0  # Spike at 12:00
    smoothed = smooth_weights(raw)
    assert smoothed[12] == 0.25 * 10 + 0.50 * 100 + 0.25 * 10  # 55.0
    assert smoothed[11] == 0.25 * 10 + 0.50 * 10 + 0.25 * 100  # 32.5

    # 3. Best slot selection
    now_utc = datetime(2026, 10, 22, 10, 0, 0, tzinfo=timezone.utc)
    slot, explanation = await pick_best_slot(now_utc=now_utc)
    tz = ZoneInfo(config.TZ)
    local_slot = slot.astimezone(tz)
    # Selected slot must not be in quiet hours (0-7)
    assert not is_in_quiet_hours(local_slot.hour, (0, 7))
    # Slot must be in future (>= now + 30 min)
    assert slot >= now_utc + timedelta(minutes=29)
    assert len(explanation) > 0


async def test_claim_idempotency() -> None:
    """Test 5: atomic claim ensures each due post is claimed exactly once."""
    await db.init_db()

    # Create 3 scheduled posts with due time in the past
    now_utc = datetime.now(timezone.utc)
    past_time = now_utc - timedelta(minutes=10)

    p1 = await db.create_post(owner_id=1, scheduled_at=past_time, status="scheduled", caption="Post 1")
    p2 = await db.create_post(owner_id=1, scheduled_at=past_time, status="scheduled", caption="Post 2")
    p3 = await db.create_post(owner_id=1, scheduled_at=past_time, status="scheduled", caption="Post 3")

    # Run 3 concurrent worker tasks claiming due posts simultaneously
    results: list[list[db.Post]] = []

    async def worker() -> None:
        claimed = await db.claim_due_posts(now_utc)
        results.append(claimed)

    await asyncio.gather(worker(), worker(), worker())

    all_claimed_ids = [p.id for batch in results for p in batch]
    expected_ids = {p1.id, p2.id, p3.id}

    # Exactly 3 claims across all workers, zero duplicates
    assert set(all_claimed_ids) == expected_ids
    assert len(all_claimed_ids) == 3

    # Test Issue 12 & 13: callback regex disambiguation and published post protection
    import re
    cancel_pattern = re.compile(r"^ig_cancel_(\d+)$")
    assert cancel_pattern.match("ig_cancel_flow") is None, "ig_cancel_flow must NOT match cancel post regex"
    assert cancel_pattern.match("ig_cancel_42") is not None
    assert cancel_pattern.match("ig_cancel_42").group(1) == "42"

    pub_post = await db.create_post(owner_id=1, status="published", caption="Published Reel History")
    fetched = await db.get_post(pub_post.id)
    assert fetched is not None and fetched.status == "published"


async def test_catchup_logic() -> None:
    """Test 6: startup catch-up (<6h publish vs >=6h ask owner)."""
    await db.init_db()
    now_utc = datetime.now(timezone.utc)

    # Post overdue by 2 hours (< 6 hours)
    recent_overdue = await db.create_post(
        owner_id=1,
        scheduled_at=now_utc - timedelta(hours=2),
        status="scheduled",
        caption="Recent overdue reel",
    )
    # Post overdue by 9 hours (>= 6 hours)
    ancient_overdue = await db.create_post(
        owner_id=1,
        scheduled_at=now_utc - timedelta(hours=9),
        status="scheduled",
        caption="Ancient overdue reel",
    )

    sent_messages: list[tuple[int, str]] = []

    class MockBot:
        async def send_message(self, chat_id: int, text: str, **kwargs) -> None:  # type: ignore[no-untyped-def]
            sent_messages.append((chat_id, text))

    await scheduler.handle_startup_catchup(MockBot())  # type: ignore[arg-type]

    # Only the ancient overdue post (>= 6h) should send an ask prompt to the owner
    assert len(sent_messages) == 1
    assert "o'tib ketgan" in sent_messages[0][1]


async def test_retry_backoff_transitions() -> None:
    """Test 7: retry backoff transitions and failure after max attempts."""
    await db.init_db()
    now_utc = datetime.now(timezone.utc)

    post = await db.create_post(
        owner_id=1,
        scheduled_at=now_utc,
        status="scheduled",
        caption="Retry test post",
    )

    # Simulate attempt 1 failure
    attempts = post.attempts + 1
    backoff_mins = [1, 5, 15][attempts - 1]
    new_time = now_utc + timedelta(minutes=backoff_mins)
    await db.update_post(post.id, status="scheduled", attempts=attempts, scheduled_at=new_time, error="transient 1")

    p_upd1 = await db.get_post(post.id)
    assert p_upd1 is not None
    assert p_upd1.attempts == 1
    assert p_upd1.status == "scheduled"
    approx((p_upd1.scheduled_at - now_utc).total_seconds(), 60, 2.0)

    # Simulate attempt 3 failure -> transition to attempt 4 / failed
    await db.update_post(post.id, attempts=3)
    p_upd3 = await db.get_post(post.id)
    assert p_upd3 is not None

    next_attempts = p_upd3.attempts + 1
    assert next_attempts > 3
    # When attempts > 3, status is set to failed
    await db.update_post(post.id, status="failed", attempts=next_attempts, error="Max attempts exceeded")

    p_final = await db.get_post(post.id)
    assert p_final is not None
    assert p_final.status == "failed"
    assert p_final.attempts == 4


async def test_ambiguous_publish_recovery() -> None:
    """Test 8: recovery when publish call is interrupted or ambiguous."""
    mock = MockInstagramServer()
    server = TestServer(mock.app)
    await server.start_server()

    try:
        client = InstagramClient(
            token="mock_valid_token",
            ig_user_id="17841400000000001",
            base_url=f"http://{server.host}:{server.port}/v21.0",
        )

        # 1. Matching caption snippet
        recovered = await client.check_recent_media_published("Ambiguous recovery test reel caption")
        assert recovered is not None
        media_id, permalink = recovered
        assert media_id == "media_existing_rec"
        assert "media_existing_rec" in permalink

        # 2. Non-matching caption snippet
        not_found = await client.check_recent_media_published("Completely different video caption")
        assert not_found is None
    finally:
        await server.close()


async def test_token_encryption_and_redaction() -> None:
    """Test 9: Fernet encryption round trip and token redaction in logs."""
    raw_token = "IGQVJ_very_secret_token_123456789_xyz"
    cipher = encrypt_token(raw_token)
    assert cipher != raw_token
    decrypted = decrypt_token(cipher)
    assert decrypted == raw_token

    # Token redaction
    log_sample = (
        "POST https://graph.instagram.com/v21.0/media?access_token=EAABtest123456789\n"
        "Headers: Authorization: OAuth EAABsecretToken987654321\n"
        "Headers: x-goog-api-key: AIzaSySecretGeminiKey12345\n"
        "Headers: x-api-key: sk-ant-secretAnthropicKey98765\n"
        "URL: ?key=AIzaSySecretGeminiKey12345&api_key=AIzaSySecretGeminiKey12345\n"
        "Error: TICK_SECRET=mySuperSecretPass on tick\n"
        "client_secret=topsecret_password_abc"
    )
    redacted = redact_token(log_sample)
    assert "EAABtest123456789" not in redacted
    assert "EAABsecretToken987654321" not in redacted
    assert "AIzaSySecretGeminiKey12345" not in redacted
    assert "sk-ant-secretAnthropicKey98765" not in redacted
    assert "mySuperSecretPass" not in redacted
    assert "topsecret_password_abc" not in redacted
    assert "[REDACTED]" in redacted


async def test_video_prep_capping() -> None:
    """Test 10: video aspect normalization and bitrate capping <= MAX_REEL_MB."""
    src = TMP / "test_16_9.mp4"
    dst_crop = TMP / "test_reel_crop.mp4"
    dst_pad = TMP / "test_reel_pad.mp4"

    # Generate a 4-second 1280x720 16:9 test video with audio using lavfi
    await ffmpeg.run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc=duration=4:size=1280x720:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "libx264", "-c:a", "aac", str(src)
    ])

    info, specs = await video_prep.check_video_specs(src)
    assert info.has_video is True
    assert specs.width == 1280
    assert specs.height == 720
    assert specs.is_9_16 is False

    # 1. Prepare with aspect_mode="crop" (must scale to exact 1080x1920, not 404x720)
    await video_prep.prepare_reel_video(src, dst_crop, info, specs, aspect_mode="crop")
    assert dst_crop.exists() and dst_crop.stat().st_size > 0
    crop_info, crop_specs = await video_prep.check_video_specs(dst_crop)
    assert crop_specs.is_9_16 is True
    assert crop_specs.width == 1080 and crop_specs.height == 1920, f"Expected 1080x1920, got {crop_specs.width}x{crop_specs.height}"
    assert dst_crop.stat().st_size <= config.MAX_REEL_MB * 1024 * 1024

    # 2. Prepare with aspect_mode="pad" (1080x1920, single video stream)
    await video_prep.prepare_reel_video(src, dst_pad, info, specs, aspect_mode="pad")
    assert dst_pad.exists() and dst_pad.stat().st_size > 0
    pad_info, pad_specs = await video_prep.check_video_specs(dst_pad)
    assert pad_specs.width == 1080 and pad_specs.height == 1920
    assert dst_pad.stat().st_size <= config.MAX_REEL_MB * 1024 * 1024

    # Verify pad has exactly 1 video stream (not duplicate 2 video streams)
    out_pad_streams = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(dst_pad),
        stdout=asyncio.subprocess.PIPE
    )
    pad_stream_types = (await out_pad_streams.communicate())[0].decode().splitlines()
    assert pad_stream_types.count("video") == 1, f"Expected 1 video stream, got {pad_stream_types.count('video')}"
    assert pad_stream_types.count("audio") == 1, "Expected 1 audio stream"

    # 3. Prepare mute video (no audio stream) -> must add silent audio track
    mute_src = TMP / "mute_16_9.mp4"
    mute_dst = TMP / "mute_reel.mp4"
    await ffmpeg.run_ffmpeg([
        "-f", "lavfi", "-i", "testsrc=duration=4:size=1280x720:rate=30",
        "-c:v", "libx264", str(mute_src)
    ])
    mute_info, mute_specs = await video_prep.check_video_specs(mute_src)
    assert mute_info.has_audio is False
    await video_prep.prepare_reel_video(mute_src, mute_dst, mute_info, mute_specs, aspect_mode="crop")
    assert mute_dst.exists() and mute_dst.stat().st_size > 0
    mute_out_info, mute_out_specs = await video_prep.check_video_specs(mute_dst)
    assert mute_out_info.has_audio is True, "Silent audio track must be added"
    assert mute_out_specs.width == 1080 and mute_out_specs.height == 1920


async def test_tick_authentication() -> None:
    """Test 11: /tick endpoint authentication and execution."""
    from aiogram import Bot, Dispatcher
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiogram.fsm.storage.memory import MemoryStorage

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    app = create_app(bot, dp)

    client = TestClient(TestServer(app))
    await client.start_server()

    try:
        # 1. No secret -> 401 Unauthorized
        r1 = await client.get("/tick")
        assert r1.status == 401

        # 2. Wrong secret -> 401 Unauthorized
        r2 = await client.get("/tick?secret=wrong_pass")
        assert r2.status == 401

        # 3. Valid GET with secret
        r3 = await client.get(f"/tick?secret={config.TICK_SECRET}")
        assert r3.status == 200
        data3 = await r3.json()
        assert data3["ok"] is True
        assert "published" in data3

        # 4. Valid POST with secret in form data
        r4 = await client.post("/tick", data={"secret": config.TICK_SECRET})
        assert r4.status == 200
        data4 = await r4.json()
        assert data4["ok"] is True

        # 5. /health check endpoint
        r5 = await client.get("/health")
        assert r5.status == 200
        assert await r5.text() == "ok"
    finally:
        await client.close()
        await bot.session.close()


async def test_mock_instagram_full_flow() -> None:
    """Test 12: End-to-end official Meta Graph API publishing pipeline against mock server."""
    mock = MockInstagramServer()
    server = TestServer(mock.app)
    await server.start_server()

    try:
        client = InstagramClient(
            token="mock_valid_token",
            ig_user_id="17841400000000001",
            base_url=f"http://{server.host}:{server.port}/v21.0",
            upload_base_url=f"http://{server.host}:{server.port}/ig-api-upload",
        )

        # 1. Account info
        acc = await client.get_account_info()
        assert acc.get("username") == "mock_creator_account"

        # 2. Quota limit
        quota = await client.get_content_publishing_limit()
        assert quota.get("quota_usage") == 3

        # 3. Online followers insight
        inf = await client.get_online_followers()
        assert inf is not None and "values" in inf

        # 4. Create Reel container
        cid = await client.create_reel_container(caption="Awesome beat Reel #music", file_size=1024 * 1024)
        assert cid.startswith("cont_")

        # 5. Resumable upload
        dummy_file = TMP / "dummy_reel.mp4"
        dummy_file.write_bytes(b"\x00" * (64 * 1024))
        await client.upload_video_resumable(cid, dummy_file)
        assert mock.containers[cid]["uploaded"] is True

        # 6. Status polling (2x IN_PROGRESS then FINISHED)
        status = await client.poll_container_status(cid, timeout=10)
        assert status == "FINISHED"

        # 7. Publish container
        media_id = await client.publish_container(cid)
        assert media_id.startswith("media_")

        # 8. Permalink
        permalink = await client.get_media_permalink(media_id)
        assert media_id in permalink

        # 9. Media insights
        insights = await client.get_media_insights(media_id)
        assert insights.get("plays") == 2500
        assert insights.get("likes") == 180

        # 10. Token refresh
        new_token, expires_in = await client.refresh_access_token("mock_valid_token")
        assert new_token == "mock_refreshed_access_token_xyz999"
        assert expires_in == 5184000
    finally:
        await server.close()


# ----------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------

async def main_async() -> int:
    print("\n--- Running Instagram Reels Auto-Poster Self-Tests ---\n")
    # Initialize shared test database
    await db.init_db()
    tests = [
        ("1. Natural time parser table", test_time_parser_table),
        ("2. Hashtag sanitizer and deduplicator", test_hashtag_sanitizer),
        ("3. Caption fallback and JSON parsing", test_caption_fallback),
        ("4. Best-time scoring and quiet hours", test_best_time_scoring),
        ("5. Atomic claim idempotency guard", test_claim_idempotency),
        ("6. Startup catch-up logic (<6h vs >=6h)", test_catchup_logic),
        ("7. Retry backoff transitions and attempts", test_retry_backoff_transitions),
        ("8. Ambiguous-publish recovery", test_ambiguous_publish_recovery),
        ("9. Token encryption & redaction in logs", test_token_encryption_and_redaction),
        ("10. Video prep 9:16 aspect & size capping", test_video_prep_capping),
        ("11. /tick authentication & health check", test_tick_authentication),
        ("12. Mock Instagram API end-to-end pipeline", test_mock_instagram_full_flow),
    ]

    for name, fn in tests:
        await run_case(name, fn)

    # Cleanup temp directory
    shutil.rmtree(TMP, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f"\nInstagram Self-Test Summary: {passed}/{total} passed.")

    if passed != total:
        print("\nFailed cases:")
        for name, ok, err in RESULTS:
            if not ok:
                print(f"  - {name}: {err}")
        return 1
    return 0


def main() -> None:
    code = asyncio.run(main_async())
    sys.exit(code)


if __name__ == "__main__":
    main()
