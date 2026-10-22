"""Follower activity analysis, optimal slot calculation, and manual time parsing."""
from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import config
from services import db

if TYPE_CHECKING:
    from services.instagram import InstagramClient

log = logging.getLogger(__name__)

# Fallback hourly activity weights (0 to 23 local hours)
# Peak: 19:00 - 22:59, Secondary peak: 12:00 - 13:59, Low: 00:00 - 07:00
DEFAULT_HOURLY_WEIGHTS: list[float] = [
    10.0, 5.0, 5.0, 5.0, 5.0, 10.0, 20.0, 40.0,   # 00:00 - 07:59
    65.0, 70.0, 75.0, 80.0, 90.0, 85.0, 70.0, 75.0, # 08:00 - 15:59
    80.0, 85.0, 90.0, 100.0, 100.0, 95.0, 85.0, 45.0  # 16:00 - 23:59
]


def parse_quiet_hours(raw: str) -> tuple[int, int]:
    """Parse '0-7' into (0, 7)."""
    m = re.match(r"^(\d{1,2})\s*-\s*(\d{1,2})$", raw.strip())
    if m:
        return int(m.group(1)), int(m.group(2))
    return 0, 7


def is_in_quiet_hours(hour: int, quiet_range: tuple[int, int]) -> bool:
    """Check if a local hour falls into the quiet window."""
    start, end = quiet_range
    if start <= end:
        return start <= hour <= end
    # Wrap-around midnight (e.g. 23-6)
    return hour >= start or hour <= end


def smooth_weights(weights: list[float]) -> list[float]:
    """Smooth hourly scores with 0.25 / 0.50 / 0.25 weights."""
    n = len(weights)
    if n != 24:
        return weights
    smoothed = [0.0] * 24
    for h in range(24):
        prev_h = (h - 1) % 24
        next_h = (h + 1) % 24
        smoothed[h] = 0.25 * weights[prev_h] + 0.50 * weights[h] + 0.25 * weights[next_h]
    return smoothed


async def get_smoothed_heatmap(client: InstagramClient | None = None) -> tuple[list[float], bool]:
    """Return (smoothed_hourly_weights, is_real_insight). Cached in kv for 24h."""
    now_ts = time.time()
    cached = await db.get_kv("insights_heatmap", None)
    if cached and isinstance(cached, dict):
        fetched_at = cached.get("fetched_at", 0)
        weights = cached.get("weights")
        if now_ts - fetched_at < 24 * 3600 and isinstance(weights, list) and len(weights) == 24:
            return smooth_weights([float(x) for x in weights]), True

    # Try fetching fresh insight if client is available
    if client and client.token:
        try:
            insight_data = await client.get_online_followers()
            if insight_data and "values" in insight_data:
                vals = insight_data["values"]
                if vals and "value" in vals[0]:
                    v_dict = vals[0]["value"]
                    if isinstance(v_dict, dict) and len(v_dict) >= 20:
                        raw_weights = [float(v_dict.get(str(h), 0)) for h in range(24)]
                        # Cache raw weights in db
                        await db.set_kv("insights_heatmap", {"weights": raw_weights, "fetched_at": now_ts})
                        log.info("cached fresh online_followers heatmap from Instagram")
                        return smooth_weights(raw_weights), True
        except Exception as exc:
            log.warning("failed to get online followers from API: %s", exc)

    return smooth_weights(DEFAULT_HOURLY_WEIGHTS), False


async def pick_best_slot(
    client: InstagramClient | None = None,
    now_utc: datetime | None = None,
) -> tuple[datetime, str]:
    """Pick optimal posting slot in next 36 hours respecting quiet hours, gap, and daily limits."""
    now = now_utc or datetime.now(timezone.utc)
    tz = ZoneInfo(config.TZ)
    quiet_range = parse_quiet_hours(config.QUIET_HOURS)

    weights, is_insight = await get_smoothed_heatmap(client)
    scheduled_posts = await db.list_scheduled_posts(limit=100)

    # Pre-calculate existing scheduled dates and times
    existing_datetimes = [p.scheduled_at for p in scheduled_posts if p.scheduled_at is not None]
    posts_by_date: dict[str, int] = {}
    for dt in existing_datetimes:
        local_date_str = dt.astimezone(tz).strftime("%Y-%m-%d")
        posts_by_date[local_date_str] = posts_by_date.get(local_date_str, 0) + 1

    # Candidate hours: 1 to 36 hours ahead
    min_start = now + timedelta(minutes=10)
    best_slot: datetime | None = None
    best_score = -1.0

    # Align starting check to next full hour
    start_hour = min_start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

    for i in range(36):
        candidate_utc = start_hour + timedelta(hours=i)
        candidate_local = candidate_utc.astimezone(tz)
        local_hour = candidate_local.hour
        local_date_str = candidate_local.strftime("%Y-%m-%d")

        # 1. Quiet hours filter
        if is_in_quiet_hours(local_hour, quiet_range):
            continue

        # 2. Daily post cap filter
        if posts_by_date.get(local_date_str, 0) >= config.MAX_POSTS_PER_DAY:
            continue

        # 3. Minimum gap between posts filter
        too_close = False
        for ex_dt in existing_datetimes:
            diff_hours = abs((candidate_utc - ex_dt).total_seconds()) / 3600.0
            if diff_hours < config.MIN_GAP_HOURS:
                too_close = True
                break
        if too_close:
            continue

        score = weights[local_hour]
        if score > best_score:
            best_score = score
            best_slot = candidate_utc

    # Fallback if all 36h slots were filtered out: 4 hours from now outside quiet hours
    if best_slot is None:
        candidate = min_start + timedelta(hours=config.MIN_GAP_HOURS)
        while is_in_quiet_hours(candidate.astimezone(tz).hour, quiet_range):
            candidate += timedelta(hours=1)
        best_slot = candidate

    # Add 0-9 minute organic jitter
    jitter_min = random.randint(0, 9)
    best_slot = best_slot + timedelta(minutes=jitter_min)

    local_dt = best_slot.astimezone(tz)
    local_time_str = local_dt.strftime("%H:%M")

    if is_insight:
        explanation = f"Obunachilaringiz {local_time_str} atrofida eng faol (Instagram tahlili asosida)."
    else:
        explanation = f"Eng yuqori qamrov vaqti tanlandi: {local_time_str} (standart faollik tavsiyasi)."

    return best_slot, explanation


def parse_scheduled_time(text: str, now_utc: datetime | None = None) -> datetime:
    """Parse manual datetime string in Uzbek / Russian / standard format into UTC datetime."""
    now = now_utc or datetime.now(timezone.utc)
    tz = ZoneInfo(config.TZ)
    local_now = now.astimezone(tz)
    s = text.strip().lower()

    # Relative: "+2h", "+2 s", "2 soatdan keyin", "+30m", "30 daqiqadan keyin"
    m_rel = re.match(r"^\+?\s*(\d+)\s*(h|soat|s|hour|hours)(?:dan\s*keyin)?$", s)
    if m_rel:
        hours = int(m_rel.group(1))
        target_utc = now + timedelta(hours=hours)
        if target_utc > now + timedelta(days=30):
            raise ValueError("time too far in future")
        return target_utc

    m_min = re.match(r"^\+?\s*(\d+)\s*(m|daqiqa|min|minutes)(?:dan\s*keyin)?$", s)
    if m_min:
        mins = int(m_min.group(1))
        target_utc = now + timedelta(minutes=mins)
        if target_utc <= now:
            raise ValueError("time in the past")
        return target_utc

    # "bugun 20:30"
    m_today = re.match(r"^(?:bugun|today)\s+(\d{1,2})[:.](\d{2})$", s)
    if m_today:
        h, m = int(m_today.group(1)), int(m_today.group(2))
        local_target = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        target_utc = local_target.astimezone(timezone.utc)
        if target_utc <= now:
            raise ValueError("time in the past")
        return target_utc

    # "ertaga 19:00"
    m_tomorrow = re.match(r"^(?:ertaga|tomorrow)\s+(\d{1,2})[:.](\d{2})$", s)
    if m_tomorrow:
        h, m = int(m_tomorrow.group(1)), int(m_tomorrow.group(2))
        local_target = (local_now + timedelta(days=1)).replace(hour=h, minute=m, second=0, microsecond=0)
        return local_target.astimezone(timezone.utc)

    # "25.10 21:00" or "25.10.2026 21:00"
    m_date = re.match(r"^(\d{1,2})[./](\d{1,2})(?:[./](\d{4}))?\s+(\d{1,2})[:.](\d{2})$", s)
    if m_date:
        day = int(m_date.group(1))
        month = int(m_date.group(2))
        year = int(m_date.group(3)) if m_date.group(3) else local_now.year
        h = int(m_date.group(4))
        m = int(m_date.group(5))
        try:
            local_target = datetime(year, month, day, h, m, tzinfo=tz)
        except ValueError as exc:
            raise ValueError("invalid date values") from exc
        target_utc = local_target.astimezone(timezone.utc)
        if target_utc <= now:
            raise ValueError("time in the past")
        if target_utc > now + timedelta(days=30):
            raise ValueError("time too far in future")
        return target_utc

    # "2026-10-25 21:00"
    m_iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})\s+(\d{1,2})[:.](\d{2})$", s)
    if m_iso:
        year, month, day, h, m = (int(x) for x in m_iso.groups())
        try:
            local_target = datetime(year, month, day, h, m, tzinfo=tz)
        except ValueError as exc:
            raise ValueError("invalid date values") from exc
        target_utc = local_target.astimezone(timezone.utc)
        if target_utc <= now:
            raise ValueError("time in the past")
        if target_utc > now + timedelta(days=30):
            raise ValueError("time too far in future")
        return target_utc

    # Plain time: "20:30" (if later today, today; else tomorrow)
    m_time = re.match(r"^(\d{1,2})[:.](\d{2})$", s)
    if m_time:
        h, m = int(m_time.group(1)), int(m_time.group(2))
        local_target = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        if local_target <= local_now:
            local_target += timedelta(days=1)
        return local_target.astimezone(timezone.utc)

    raise ValueError("unrecognized time format")
