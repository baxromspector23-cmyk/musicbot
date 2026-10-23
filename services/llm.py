"""AI caption and hashtag generation using lightweight REST calls to Gemini or Anthropic."""
from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

import aiohttp

import config
from services.tokens import redact_token

log = logging.getLogger(__name__)

BANNED_TAGS = frozenset({
    "like4like", "followme", "f4f", "l4l", "tagsforlikes", "instadaily",
    "follow4follow", "viral", "explorepage", "fyp", "rek", "rekomendatsii",
})

SYSTEM_PROMPT = """You are an expert social media editor writing Instagram Reels captions.
Follow these strict rules:
1. Language: {lang}. Write naturally in the requested language.
2. Hook: The very first line MUST be an engaging hook under 120 characters (Instagram truncates preview after 125 characters).
3. Length: 2 to 4 short lines total (easy to read on mobile).
4. Keywords: Naturally weave 2-3 specific search keywords into the caption text describing what happens in the video.
5. Call to Action: Include one soft call to action at the end (e.g., save, share, comment).
6. Body clean: DO NOT put any hashtags inside the body text.
7. Emojis: Use at most 3 relevant emojis total.
8. No spam: No false claims, no engagement-bait spam (e.g., 'comment 5 times to win'), no copyright lyrics.
9. Format: You MUST return a single valid JSON object with EXACTLY two keys:
   "caption": "The body text without hashtags",
   "hashtags": ["tag1", "tag2", "tag3", "tag4", "tag5"]
Do not wrap in markdown or any text outside the JSON object."""


def sanitize_hashtag(tag: str) -> str:
    """Clean a hashtag string: lowercase, no leading #, only letters, digits, and underscores."""
    t = tag.strip().lstrip("#").lower()
    return re.sub(r"[^a-z0-9_а-яёўқғҳ]", "", t)


def parse_raw_hashtags(raw_input: str | list[str]) -> list[str]:
    """Parse hashtags from string or list, splitting on commas, semicolons, whitespace, or consecutive #."""
    if isinstance(raw_input, list):
        text = " ".join(str(item) for item in raw_input)
    else:
        text = str(raw_input or "")

    # Separate attached hashes like '#tag1#tag2' -> ' #tag1 #tag2'
    text = re.sub(r"(?<=[\w\d])#", " #", text)
    # Split on whitespace, commas, semicolons
    tokens = re.split(r"[\s,;]+", text)
    result: list[str] = []
    for tok in tokens:
        cleaned = tok.strip().lstrip("#").strip(",; ")
        if cleaned:
            result.append(cleaned)
    return result


def extract_caption_body_and_tags(caption: str) -> tuple[str, list[str]]:
    """Separate caption body from hashtags, preserving all body paragraphs intact."""
    if not caption:
        return "", []

    raw = caption.strip()

    # 1. Check if the last paragraph / block consists exclusively of hashtags
    paragraphs = re.split(r"\n\s*\n", raw)
    trailing_tags: list[str] = []

    if len(paragraphs) > 1:
        last_p = paragraphs[-1].strip()
        words = last_p.split()
        if words and all(w.strip(",;").startswith("#") for w in words):
            trailing_tags = parse_raw_hashtags(last_p)
            body_paragraphs = paragraphs[:-1]
            raw_body = "\n\n".join(body_paragraphs).strip()
        else:
            raw_body = raw
    else:
        lines = raw.split("\n")
        if len(lines) > 1:
            last_line = lines[-1].strip()
            words = last_line.split()
            if words and all(w.strip(",;").startswith("#") for w in words):
                trailing_tags = parse_raw_hashtags(last_line)
                raw_body = "\n".join(lines[:-1]).strip()
            else:
                raw_body = raw
        else:
            raw_body = raw

    # 2. Extract any trailing hashtags at the end of raw_body
    end_tags_match = re.search(r"(\s*[,;]?\s*#[\wа-яёўқғҳ]+)+$", raw_body)
    if end_tags_match:
        trailing_end_str = end_tags_match.group(0)
        trailing_tags.extend(parse_raw_hashtags(trailing_end_str))
        raw_body = raw_body[:end_tags_match.start()].strip()

    # 3. Extract any remaining inline hashtags from raw_body
    inline_tags_raw = re.findall(r"#([\wа-яёўқғҳ]+)", raw_body, flags=re.IGNORECASE)

    # Clean remaining inline hashtags in body text by removing leading '#' so text remains natural
    clean_body = re.sub(r"#([\wа-яёўқғҳ]+)", r"\1", raw_body).strip()

    # Combine trailing tags and inline tags (preserving order, deduplicated)
    all_tags: list[str] = []
    seen: set[str] = set()
    for t in trailing_tags + inline_tags_raw:
        clean = sanitize_hashtag(t)
        if clean and clean not in seen and len(clean) >= 2:
            seen.add(clean)
            all_tags.append(clean)

    return clean_body, all_tags


def validate_and_format_hashtags(
    tags: list[str] | str,
    brand_tag: str = "",
    default_tags: list[str] | None = None,
    limit: int = 5,
) -> list[str]:
    """Ensure at most `limit` unique, valid, non-banned hashtags, properly handling commas and lists."""
    parsed_tags = parse_raw_hashtags(tags)
    seen: set[str] = set()
    result: list[str] = []

    # Include brand tag first if present
    if brand_tag:
        clean_brand = sanitize_hashtag(brand_tag)
        if clean_brand and clean_brand not in BANNED_TAGS and len(clean_brand) >= 2:
            seen.add(clean_brand)
            result.append(clean_brand)

    for raw in parsed_tags:
        clean = sanitize_hashtag(raw)
        if clean and clean not in seen and clean not in BANNED_TAGS and len(clean) >= 2:
            seen.add(clean)
            result.append(clean)
            if len(result) >= limit:
                break

    # If too few, supplement with default tags
    if len(result) < limit and default_tags:
        for d in default_tags:
            for parsed_d in parse_raw_hashtags(d):
                clean = sanitize_hashtag(parsed_d)
                if clean and clean not in seen and clean not in BANNED_TAGS and len(clean) >= 2:
                    seen.add(clean)
                    result.append(clean)
                    if len(result) >= limit:
                        break
            if len(result) >= limit:
                break

    return result[:limit]


def build_fallback_caption(
    description: str,
    brand_tag: str = "",
    default_tags: list[str] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Deterministic fallback caption if AI is unavailable."""
    desc = description.strip()
    if not desc or desc.lower() == "avto":
        body = "Yangi video lavha! Ko'ring, baho bering va saqlab qo'ying 🎬"
    else:
        body = f"{desc}\n\nFikringizni izohlarda qoldiring va do'stlaringizga ulashing ✨"

    hashtags = validate_and_format_hashtags([], brand_tag, default_tags or config.DEFAULT_HASHTAGS, limit)
    full_text = f"{body}\n\n{' '.join('#' + t for t in hashtags)}".strip()
    return {
        "caption": body,
        "hashtags": hashtags,
        "full_text": full_text[:2200],
        "is_fallback": True,
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    """Extract and parse JSON object from LLM response text."""
    # Strip markdown codeblocks
    cleaned = re.sub(r"^```(?:json)?\s*", "", text.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict) and "caption" in obj:
            return obj
    except Exception:
        # Search for first { and last }
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict) and "caption" in obj:
                    return obj
            except Exception:
                pass
    return None


async def _call_gemini(
    prompt: str,
    frames: list[bytes],
    api_key: str,
    model: str,
    lang: str,
    base_url: str = "https://generativelanguage.googleapis.com/v1beta",
) -> str:
    """Call Google Gemini REST generateContent API with automatic model fallback."""
    models_to_try = [model]
    for fb in ("gemini-2.5-flash", "gemini-3-flash-preview"):
        if fb not in models_to_try:
            models_to_try.append(fb)

    parts: list[dict[str, Any]] = []

    # Add images
    for f in frames[:3]:
        b64 = base64.b64encode(f).decode("ascii")
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": b64}})

    parts.append({"text": prompt})
    system_text = SYSTEM_PROMPT.format(lang=lang)

    payload = {
        "system_instruction": {"parts": [{"text": system_text}]},
        "contents": [{"parts": parts}],
        "generationConfig": {
            "temperature": 0.6,
            "response_mime_type": "application/json",
        },
    }

    last_error: Exception | None = None
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
        for m in models_to_try:
            url = f"{base_url}/models/{m}:generateContent"
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candidates = data.get("candidates", [])
                        if candidates:
                            part = candidates[0].get("content", {}).get("parts", [{}])[0]
                            return part.get("text", "")
                    err_text = await resp.text()
                    log.warning("Gemini model %s returned status %d: %s", m, resp.status, redact_token(err_text[:150]))
                    last_error = ValueError(f"Gemini API {m} returned {resp.status}")
            except Exception as exc:
                log.warning("Gemini model %s error: %s", m, redact_token(str(exc)))
                last_error = exc

    raise last_error or ValueError("All Gemini models failed")


async def _call_anthropic(
    prompt: str,
    frames: list[bytes],
    api_key: str,
    model: str,
    lang: str,
) -> str:
    """Call Anthropic Messages REST API."""
    url = "https://api.anthropic.com/v1/messages"
    content_list: list[dict[str, Any]] = []

    for f in frames[:3]:
        b64 = base64.b64encode(f).decode("ascii")
        content_list.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
        })

    content_list.append({"type": "text", "text": prompt})
    system_text = SYSTEM_PROMPT.format(lang=lang)

    payload = {
        "model": model,
        "max_tokens": 600,
        "system": system_text,
        "messages": [{"role": "user", "content": content_list}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status != 200:
                err_text = await resp.text()
                raise ValueError(f"Anthropic API returned status {resp.status}: {redact_token(err_text[:200])}")
            data = await resp.json()
            parts = data.get("content", [])
            if not parts:
                raise ValueError("Anthropic returned empty content")
            return parts[0].get("text", "")


async def generate_caption(
    description: str,
    audio_tags: dict[str, str] | None = None,
    frames: list[bytes] | None = None,
    lang: str = "",
    brand_tag: str = "",
    default_tags: list[str] | None = None,
    limit: int = 5,
) -> dict[str, Any]:
    """Generate search-keyword-optimized caption and hashtags using LLM with retry and fallback."""
    lang = lang or config.CAPTION_LANG
    brand_tag = brand_tag or config.BRAND_HASHTAG
    default_tags = default_tags or config.DEFAULT_HASHTAGS
    frames = frames or []
    audio_tags = audio_tags or {}

    provider = config.LLM_PROVIDER
    gemini_key = config.GEMINI_API_KEY
    anthropic_key = config.ANTHROPIC_API_KEY

    # Check if a usable API key exists
    has_api = (provider == "gemini" and gemini_key) or (provider == "anthropic" and anthropic_key)
    if not has_api:
        # Check if the other provider has an API key as fallback
        if gemini_key:
            provider = "gemini"
        elif anthropic_key:
            provider = "anthropic"
        else:
            log.info("no LLM API key configured; using template fallback")
            return build_fallback_caption(description, brand_tag, default_tags, limit)

    prompt = f"Description: {description.strip() or 'Video'}\n"
    if audio_tags.get("title") or audio_tags.get("artist"):
        prompt += f"Audio: {audio_tags.get('artist', '')} - {audio_tags.get('title', '')}\n"
    prompt += "Write the JSON caption and hashtags now."

    # Try up to 2 times
    for attempt in range(2):
        try:
            if provider == "gemini":
                raw_text = await _call_gemini(prompt, frames, gemini_key, config.LLM_MODEL, lang)
            else:
                raw_text = await _call_anthropic(prompt, frames, anthropic_key, config.LLM_MODEL, lang)

            parsed = _extract_json(raw_text)
            if parsed and "caption" in parsed:
                caption_body = str(parsed["caption"]).strip()
                raw_tags = parsed.get("hashtags", [])
                if isinstance(raw_tags, list):
                    clean_tags = validate_and_format_hashtags(
                        [str(t) for t in raw_tags], brand_tag, default_tags, limit
                    )
                else:
                    clean_tags = validate_and_format_hashtags([], brand_tag, default_tags, limit)

                full_text = f"{caption_body}\n\n{' '.join('#' + t for t in clean_tags)}".strip()
                return {
                    "caption": caption_body,
                    "hashtags": clean_tags,
                    "full_text": full_text[:2200],
                    "is_fallback": False,
                }
        except Exception as exc:
            log.warning("LLM attempt %d failed: %s", attempt + 1, exc)

    log.info("LLM generation failed after retries; using fallback caption")
    return build_fallback_caption(description, brand_tag, default_tags, limit)
