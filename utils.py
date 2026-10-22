"""Pure helpers: time parsing, formatting, filename sanitizing."""
from __future__ import annotations

import re
import unicodedata

import config

_END_WORDS = {"oxir", "end"}
_SEG_RE = re.compile(r"^\d+$")
_LAST_RE = re.compile(r"^(\d+(\.\d+)?|\.\d+)$")


class UserError(Exception):
    """Error whose message is safe to show to the user."""


def parse_time(text: str) -> float:
    """Parse '83', '83.5', '1:23', '01:23.5', '1:02:03' (comma decimals ok) to seconds."""
    s = text.strip().replace(",", ".")
    if not s:
        raise ValueError("empty time")
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError("too many parts")
    for p in parts[:-1]:
        if not _SEG_RE.match(p):
            raise ValueError("bad segment")
    if not _LAST_RE.match(parts[-1]):
        raise ValueError("bad seconds")
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def _split_range(text: str) -> list[str]:
    s = text.strip()
    tokens = [t.strip(",") for t in re.split(r"\s*[-–—→]+\s*|\s+", s) if t.strip(",")]
    if len(tokens) == 1 and "," in tokens[0]:
        left, _, right = tokens[0].partition(",")
        is_decimal = len(right) <= 2 and right.isdigit() and ":" not in right
        if not is_decimal and left and right:
            return [left, right]
    return tokens


def parse_range(text: str) -> tuple[float, float | None]:
    """Parse 'start end' into (start, end); end None means end of file."""
    if text.strip().startswith("-"):
        raise ValueError("negative start")
    tokens = _split_range(text)
    if not 1 <= len(tokens) <= 2:
        raise ValueError("need one or two times")
    if tokens[0].lower() in _END_WORDS:
        raise ValueError("start cannot be end")
    start = parse_time(tokens[0])
    end: float | None = None
    if len(tokens) == 2:
        end = None if tokens[1].lower() in _END_WORDS else parse_time(tokens[1])
    return start, end


def validate_range(start: float, end: float | None, duration: float) -> tuple[float, float]:
    """Validate and clamp a cut range; raise ValueError when invalid."""
    if end is None:
        end = duration
    if start < 0 or end <= start:
        raise ValueError("bad order")
    if end > duration + config.END_TOLERANCE_SEC:
        raise ValueError("end beyond file")
    end = min(end, duration)
    if start >= duration or end - start < config.MIN_CLIP_SEC:
        raise ValueError("clip too short")
    return start, end


def parse_float(text: str, lo: float, hi: float) -> float:
    """Parse a decimal (comma allowed) within [lo, hi]."""
    s = text.strip().replace(",", ".")
    if not re.fullmatch(r"\d+(\.\d+)?|\.\d+", s):
        raise ValueError("not a number")
    v = float(s)
    if not lo <= v <= hi:
        raise ValueError("out of range")
    return v


def parse_int(text: str, lo: int, hi: int) -> int:
    """Parse a signed integer within [lo, hi]."""
    s = text.strip()
    if not re.fullmatch(r"[+-]?\d+", s):
        raise ValueError("not an integer")
    v = int(s)
    if not lo <= v <= hi:
        raise ValueError("out of range")
    return v


def parse_volume(text: str) -> float:
    """Parse '+20', '-30%', '150%' into a gain factor within 0.1-4.0."""
    m = re.fullmatch(r"\s*([+-])?\s*(\d+(?:[.,]\d+)?)\s*%?\s*", text)
    if not m:
        raise ValueError("bad volume")
    val = float(m.group(2).replace(",", "."))
    sign = m.group(1)
    factor = 1 + val / 100 if sign == "+" else 1 - val / 100 if sign == "-" else val / 100
    factor = round(factor, 4)
    if not 0.1 <= factor <= 4.0:
        raise ValueError("volume out of range")
    return factor


def fade_limit(mode: str, duration: float) -> float:
    """Maximum allowed fade length for a mode ('in', 'out', 'both')."""
    return duration / 2 if mode == "both" else duration


def fmt_duration(seconds: float) -> str:
    """Format seconds as mm:ss (or h:mm:ss for an hour or more)."""
    total = int(round(max(seconds, 0)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def fmt_size(num_bytes: int) -> str:
    """Human readable size."""
    if num_bytes >= 1024 * 1024:
        return f"{num_bytes / 1024 / 1024:.1f} MB"
    return f"{num_bytes / 1024:.0f} KB"


def fmt_num(x: float) -> str:
    """Compact number formatting (2.0 -> '2', 1.25 -> '1.25')."""
    return f"{x:.2f}".rstrip("0").rstrip(".")


def clean_text(value: str, max_len: int = config.MAX_TEXT_LEN) -> str:
    """Strip control characters, collapse whitespace and trim to max_len."""
    s = "".join(ch for ch in value if unicodedata.category(ch)[0] != "C" or ch in " ")
    return re.sub(r"\s+", " ", s).strip()[:max_len]


def sanitize_filename(name: str, max_len: int = 100) -> str:
    """Remove characters illegal in file names and cap the length."""
    s = re.sub(r'[\\/:*?"<>|]', "", name)
    s = "".join(ch for ch in s if unicodedata.category(ch)[0] != "C")
    return re.sub(r"\s+", " ", s).strip(" .")[:max_len].strip(" .")


def progress_bar(percent: float, width: int = 10) -> str:
    """Text progress bar."""
    pct = max(0.0, min(100.0, percent))
    filled = int(round(pct / 100 * width))
    return f"[{'█' * filled}{'░' * (width - filled)}] {int(pct)}%"
