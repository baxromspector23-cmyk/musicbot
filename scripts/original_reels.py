"""Original Uzbek Reels generator + publisher.

Every video is 100% generated here (no third-party footage): animated background,
Uzbek hook banner, word-by-word karaoke captions synced to Uzbek TTS (edge-tts),
and a comment-driving CTA card at the end.

Usage (run from the project root with the Windows Python that has the deps):
    python -m scripts.original_reels --render r01_fuel_arrow   # render one video
    python -m scripts.original_reels --render-all              # render all videos
    python -m scripts.original_reels --post-next               # render (if needed) + publish next unposted
    python -m scripts.original_reels --status                  # show what is posted
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import subprocess
import sys
import wave
import zlib
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PLAN_FILE = ROOT / "content" / "reels_plan.json"
OUT_DIR = ROOT / "data" / "reels_out"
STATE_FILE = ROOT / "data" / "original_reels_state.json"
LOG_FILE = OUT_DIR / "log.txt"

W, H, FPS = 1080, 1920, 30
SR = 24000
VOICE = "uz-UZ-SardorNeural"
RATE = "+8%"
HANDLE = "@bilasizmi.uz.sirlar"
MIN_HOURS_BETWEEN_POSTS = 4
SILENT = True  # no AI voice; licensed Instagram music is attached at upload
MUSIC_QUERY = {"car": "phonk", "phone": "lofi", "fact": "lofi"}

FONT_DIR = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
if (FONT_DIR / "seguibl.ttf").exists():
    FONT_BLACK = str(FONT_DIR / "seguibl.ttf")
    FONT_BOLD = str(FONT_DIR / "segoeuib.ttf")
else:  # Linux / GitHub Actions: bundled OFL fonts
    FONT_BLACK = str(ROOT / "assets" / "fonts" / "Montserrat-900.ttf")
    FONT_BOLD = str(ROOT / "assets" / "fonts" / "Montserrat-700.ttf")

YELLOW = (255, 214, 10)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (230, 30, 50)

THEMES = {
    "car": ((8, 18, 48), (0, 0, 0), (255, 120, 0)),
    "phone": ((40, 10, 70), (5, 5, 25), (0, 200, 255)),
    "fact": ((0, 60, 60), (0, 8, 12), (255, 214, 10)),
}


def log(msg: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------------------------------------------------------------- audio

async def _tts(text: str, mp3: Path) -> list[tuple[str, float, float]]:
    import edge_tts

    words: list[tuple[str, float, float]] = []
    comm = edge_tts.Communicate(text, VOICE, rate=RATE, boundary="WordBoundary")
    with mp3.open("wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                s = ch["offset"] / 1e7
                words.append((ch["text"], s, s + ch["duration"] / 1e7))
    return words


def _decode(mp3: Path) -> np.ndarray:
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(mp3),
         "-f", "s16le", "-ac", "1", "-ar", str(SR), "-"],
        check=True, capture_output=True,
    ).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def _whoosh(dur: float = 0.45) -> np.ndarray:
    n = int(SR * dur)
    t = np.linspace(0, 1, n)
    rng = np.random.default_rng(7)
    noise = rng.standard_normal(n)
    # crude band-pass sweep: moving-average of varying width
    out = np.zeros(n)
    acc = np.cumsum(noise)
    for i in range(n):
        k = int(2 + 30 * (1 - t[i]))
        j = max(0, i - k)
        out[i] = (acc[i] - acc[j]) / (i - j + 1e-9)
    env = np.sin(np.pi * t) ** 2
    return (out * env * 0.25).astype(np.float32)


def build_audio(item: dict, work: Path) -> tuple[Path, list[dict], float]:
    """Synthesize all segments; return wav path, segments with timings, total duration."""
    texts = list(item["voice"]) + [item["cta"]]
    kinds = ["hook"] + ["body"] * (len(item["voice"]) - 1) + ["cta"]
    gaps = [0.30] + [0.15] * (len(item["voice"]) - 2) + [0.35, 0.0]
    lead = 0.15
    pcm_parts = [np.zeros(int(SR * lead), dtype=np.float32)]
    cursor = lead
    segs: list[dict] = []
    for i, (txt, kind) in enumerate(zip(texts, kinds)):
        mp3 = work / f"seg{i}.mp3"
        words = asyncio.run(_tts(txt, mp3))
        pcm = _decode(mp3)
        dur = len(pcm) / SR
        if not words:  # fallback: spread words proportionally
            toks = txt.split()
            total = sum(len(w) for w in toks) or 1
            acc = 0.0
            for w in toks:
                d = dur * len(w) / total
                words.append((w, acc, acc + d))
                acc += d
        segs.append({
            "kind": kind, "text": txt, "start": cursor, "end": cursor + dur,
            "words": [(w, cursor + s, cursor + e) for w, s, e in words],
        })
        pcm_parts.append(pcm)
        cursor += dur
        g = gaps[i] if i < len(gaps) else 0.0
        pcm_parts.append(np.zeros(int(SR * g), dtype=np.float32))
        cursor += g
    tail = 1.0
    pcm_parts.append(np.zeros(int(SR * tail), dtype=np.float32))
    total = cursor + tail
    mix = np.concatenate(pcm_parts)
    wh = _whoosh()
    mix[: len(wh)] += wh
    mix = np.clip(mix * 0.95, -1, 1)
    wav = work / "voice.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((mix * 32767).astype(np.int16).tobytes())
    return wav, segs, total


def build_silent(item: dict, work: Path) -> tuple[Path, list[dict], float]:
    """No voice: words appear at reading pace; returns silent wav (music is added at upload)."""
    texts = list(item["voice"]) + [item["cta"]]
    kinds = ["hook"] + ["body"] * (len(item["voice"]) - 1) + ["cta"]
    cursor = 0.0
    segs: list[dict] = []
    for txt, kind in zip(texts, kinds):
        toks = txt.split()
        if kind == "hook":
            dur = 2.4
        elif kind == "cta":
            dur = 3.2
        else:
            dur = max(2.0, 0.36 * len(toks) + 0.5)
        per = (dur - 0.4) / max(len(toks), 1)
        words = [(w, cursor + i * per, cursor + (i + 1) * per) for i, w in enumerate(toks)]
        segs.append({"kind": kind, "text": txt, "start": cursor, "end": cursor + dur, "words": words})
        cursor += dur
    total = cursor + 0.4
    wav = work / "silence.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(np.zeros(int(SR * total), dtype=np.int16).tobytes())
    return wav, segs, total


# ---------------------------------------------------------------- graphics

@lru_cache(maxsize=None)
def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _parse_marked(line: str) -> list[tuple[str, bool]]:
    """'abc *def* ghi' -> [('abc', F), ('def', T), ('ghi', F)] word list with highlight flag."""
    out: list[tuple[str, bool]] = []
    hl = False
    for tok in line.split(" "):
        if not tok:
            continue
        start = tok.startswith("*")
        end = tok.endswith("*")
        if start:
            hl = True
        out.append((tok.strip("*"), hl))
        if end:
            hl = False
    return out


def render_hook_image(hook: str, accent: tuple) -> Image.Image:
    lines = [_parse_marked(l) for l in hook.split("\n")]
    f = font(FONT_BLACK, 112)
    pad_x, line_gap = 34, 26
    space = f.getlength(" ")
    widths = [sum(f.getlength(w.upper()) for w, _ in ln) + space * (len(ln) - 1) for ln in lines]
    asc, desc = f.getmetrics()
    lh = asc + desc
    img_w = int(max(widths) + pad_x * 2 + 20)
    img_h = int(len(lines) * (lh + 24) + line_gap * (len(lines) - 1) + 20)
    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    y = 10
    for ln, lw in zip(lines, widths):
        x = (img_w - lw) / 2
        # black slab behind each line (banner look)
        d.rounded_rectangle([x - pad_x, y, x + lw + pad_x, y + lh + 24], radius=22, fill=(0, 0, 0, 225))
        for wi, (w, hl) in enumerate(ln):
            txt = w.upper()
            tw = f.getlength(txt)
            if hl:
                nxt = wi + 1 < len(ln) and ln[wi + 1][1]
                d.rounded_rectangle([x - 10, y + 8, x + tw + (space + 10 if nxt else 10), y + lh + 16], radius=14, fill=YELLOW + (255,))
                d.text((x, y + 10), txt, font=f, fill=BLACK)
            else:
                d.text((x, y + 10), txt, font=f, fill=WHITE)
            x += tw + space
        y += lh + 24 + line_gap
    maxw = W - 100
    if img.width > maxw:
        img = img.resize((maxw, int(img.height * maxw / img.width)), Image.LANCZOS)
    return img


def render_chunk_image(words: list[str], active: int) -> Image.Image:
    f = font(FONT_BLACK, 104)
    space = f.getlength(" ")
    txts = [w.upper() for w in words]
    total = sum(f.getlength(t) for t in txts) + space * (len(txts) - 1)
    if total > W - 160:  # shrink long chunks
        f = font(FONT_BLACK, int(104 * (W - 160) / total))
        space = f.getlength(" ")
        total = sum(f.getlength(t) for t in txts) + space * (len(txts) - 1)
    asc, desc = f.getmetrics()
    img = Image.new("RGBA", (int(total + 60), asc + desc + 60), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x = 30
    for i, t in enumerate(txts):
        fill = YELLOW if i == active else WHITE
        d.text((x, 30), t, font=f, fill=fill, stroke_width=9, stroke_fill=BLACK)
        x += f.getlength(t) + space
    return img


def wrap(text: str, f: ImageFont.FreeTypeFont, maxw: int) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        trial = (cur + " " + w).strip()
        if f.getlength(trial) <= maxw:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def render_cta_image(cta: str) -> Image.Image:
    f = font(FONT_BLACK, 78)
    lines = wrap(cta, f, W - 260)
    asc, desc = f.getmetrics()
    lh = asc + desc + 6
    bw = W - 140
    bh = 120 + lh * len(lines) + 120
    img = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, bw, bh], radius=48, fill=(255, 255, 255, 245))
    y = 80
    for ln in lines:
        lw = f.getlength(ln)
        d.text(((bw - lw) / 2, y), ln, font=f, fill=BLACK)
        y += lh
    f2 = font(FONT_BLACK, 64)
    lab = "IZOHDA YOZING"
    lw = f2.getlength(lab)
    d.rounded_rectangle([(bw - lw) / 2 - 30, y + 20, (bw + lw) / 2 + 30, y + 120], radius=30, fill=RED + (255,))
    d.text(((bw - lw) / 2, y + 26), lab, font=f2, fill=WHITE)
    return img


def make_chunks(seg_words: list[tuple[str, float, float]], max_words: int = 3, max_chars: int = 18):
    chunks, cur = [], []
    for w in seg_words:
        cand = cur + [w]
        if cur and (len(cand) > max_words or sum(len(x[0]) for x in cand) > max_chars):
            chunks.append(cur)
            cur = [w]
        else:
            cur = cand
    if cur:
        chunks.append(cur)
    return chunks


def paste_scaled(base: Image.Image, img: Image.Image, cx: int, cy: int, scale: float = 1.0) -> None:
    if abs(scale - 1.0) > 1e-3:
        img = img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))), Image.BILINEAR)
    base.alpha_composite(img, (int(cx - img.width / 2), int(cy - img.height / 2)))


def render_video(item: dict, force: bool = False) -> Path:
    out = OUT_DIR / f"{item['id']}.mp4"
    if out.exists() and not force:
        return out
    work = OUT_DIR / f"_work_{item['id']}"
    work.mkdir(parents=True, exist_ok=True)
    wav, segs, total = build_silent(item, work) if SILENT else build_audio(item, work)
    c1, c2, accent = THEMES.get(item.get("theme", "fact"), THEMES["fact"])

    yy = np.linspace(0, 1, H, dtype=np.float32)[:, None, None]
    xx = np.linspace(0, 1, W, dtype=np.float32)[None, :, None]
    a1, a2, ac = (np.array(c, np.float32) for c in (c1, c2, accent))
    grad_a = a1 * (1 - yy) + a2 * yy + np.zeros((1, W, 1), np.float32)
    glow = np.exp(-(((xx - 0.5) ** 2) * 6 + ((yy - 0.42) ** 2) * 10))
    grad_b = grad_a * 0.75 + ac * glow * 0.55

    rng = np.random.default_rng(zlib.crc32(item["id"].encode()))
    bokeh = [(rng.uniform(0, W), rng.uniform(0, H), rng.uniform(30, 140), rng.uniform(-25, 25),
              rng.uniform(-60, -15), rng.uniform(25, 70)) for _ in range(14)]
    hook_img = render_hook_image(item["hook"], accent)
    small_hook = hook_img.resize((int(hook_img.width * 0.42), int(hook_img.height * 0.42)), Image.BILINEAR)
    cta_img = render_cta_image(item["cta"])
    f_label = font(FONT_BLACK, 50)
    f_handle = font(FONT_BOLD, 40)
    label = item.get("label", "BILASIZMI?")

    hook_seg = segs[0]
    cta_seg = segs[-1]
    body_chunks = []
    for s in segs[1:-1]:
        for ch in make_chunks(s["words"]):
            body_chunks.append(ch)
    chunk_cache: dict = {}

    n_frames = int(total * FPS)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", str(wav), "-c:v", "libx264", "-preset", "medium", "-crf", "19",
           "-pix_fmt", "yuv420p", "-profile:v", "high", "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
           "-shortest", "-movflags", "+faststart", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    hook_until = hook_seg["end"] + (0.0 if SILENT else 0.25)

    for fi in range(n_frames):
        t = fi / FPS
        k = 0.5 + 0.5 * math.sin(t * 1.3)
        bg = (grad_a * (1 - k) + grad_b * k).astype(np.uint8)
        frame = Image.fromarray(bg, "RGB").convert("RGBA")
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for bx, by0, r, vx, vy, al in bokeh:
            cx = (bx + vx * t) % W
            cy = (by0 + vy * t) % (H + 2 * r) - r
            ld.ellipse([cx - r, cy - r, cx + r, cy + r], fill=accent + (int(al * 0.6),))
        frame.alpha_composite(layer)
        d = ImageDraw.Draw(frame)

        # progress bar
        d.rectangle([0, 0, int(W * min(1.0, t / total)), 14], fill=YELLOW)
        # label pill (top safe zone)
        lw = f_label.getlength(label)
        d.rounded_rectangle([(W - lw) / 2 - 34, 150, (W + lw) / 2 + 34, 236], radius=43, fill=RED)
        d.text(((W - lw) / 2, 156), label, font=f_label, fill=WHITE)

        if t < hook_until:
            pop = 1.0 + 0.35 * math.exp(-t * 9)
            shake = 6 * math.sin(t * 60) * math.exp(-t * 6)
            paste_scaled(frame, hook_img, int(W / 2 + shake), 860, pop)
            if t < 0.07:
                frame.alpha_composite(Image.new("RGBA", (W, H), (255, 255, 255, 140)))
        elif t < cta_seg["start"] - 0.1:
            paste_scaled(frame, small_hook, W // 2, 380)
            cur = None
            for ci, ch in enumerate(body_chunks):
                if ch[0][1] - 0.05 <= t:
                    cur = ci
            if cur is not None:
                ch = body_chunks[cur]
                active = 0
                for wi, (_, ws, _) in enumerate(ch):
                    if ws <= t:
                        active = wi
                key = (cur, active)
                if key not in chunk_cache:
                    chunk_cache[key] = render_chunk_image([w for w, _, _ in ch], active)
                age = t - ch[0][1]
                pop = 1.0 + 0.12 * math.exp(-max(age, 0) * 14)
                paste_scaled(frame, chunk_cache[key], W // 2, 1000, pop)
        else:
            age = t - (cta_seg["start"] - 0.1)
            pop = 1.0 + 0.25 * math.exp(-age * 10)
            paste_scaled(frame, cta_img, W // 2, 900, pop)
            # bouncing arrow pointing to comments
            by = 1330 + 22 * abs(math.sin(t * 5))
            d.polygon([(W / 2 - 60, by), (W / 2 + 60, by), (W / 2, by + 80)], fill=YELLOW)

        hw = f_handle.getlength(HANDLE)
        d.text(((W - hw) / 2, 1560), HANDLE, font=f_handle, fill=(255, 255, 255, 170))
        proc.stdin.write(frame.convert("RGB").tobytes())

    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg encode failed")
    for p in work.iterdir():
        p.unlink(missing_ok=True)
    work.rmdir()
    log(f"rendered {out.name} ({total:.1f}s)")
    return out


# ---------------------------------------------------------------- publishing

def load_plan() -> list[dict]:
    return json.loads(PLAN_FILE.read_text(encoding="utf-8"))


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"posted": {}}


def save_state(st: dict) -> None:
    STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")


def full_caption(item: dict) -> str:
    tags = " ".join("#" + t for t in item["hashtags"])
    return f"{item['caption']}\n\n{tags}"


def post_next(dry_run: bool = False) -> None:
    st = load_state()
    posted = st["posted"]
    if posted:
        last = max(datetime.fromisoformat(v["at"]) for v in posted.values())
        if datetime.now(timezone.utc) - last < timedelta(hours=MIN_HOURS_BETWEEN_POSTS):
            log(f"skip: last post at {last.isoformat()} (< {MIN_HOURS_BETWEEN_POSTS}h ago)")
            return
    pending = [it for it in load_plan() if it["id"] not in posted]
    if not pending:
        log("plan finished: nothing left to post")
        return
    item = pending[0]
    video = render_video(item)
    if dry_run:
        log(f"dry-run: would post {item['id']} -> {video}")
        return
    media_id, link = upload_with_music(item, video)
    posted[item["id"]] = {"media_id": media_id, "permalink": link,
                          "at": datetime.now(timezone.utc).isoformat()}
    save_state(st)
    log(f"POSTED {item['id']} -> {link}")
    # render the next one ahead of time so the next slot is fast
    if len(pending) > 1:
        try:
            render_video(pending[1])
        except Exception as exc:
            log(f"pre-render failed: {exc}")


def upload_with_music(item: dict, video: Path) -> tuple[str, str]:
    from services import ig_direct

    settings = asyncio.run(ig_direct.load_direct_settings())
    cl = ig_direct._create_client(settings)
    q = MUSIC_QUERY.get(item.get("theme", "fact"), "lofi")
    tracks = [t for t in cl.search_music(q) if t.uri][:5]
    if not tracks:
        raise RuntimeError(f"no music found for {q!r}")
    track = tracks[zlib.crc32(item["id"].encode()) % len(tracks)]
    work = OUT_DIR / f"_up_{item['id']}"
    work.mkdir(parents=True, exist_ok=True)
    audio = cl.track_download_by_url(track.uri, "track", work)
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(video)], capture_output=True, text=True, check=True).stdout.strip())
    start = (track.highlight_start_times_in_ms or [0])[0] / 1000
    adur = (track.duration_in_ms or 0) / 1000
    if adur and start + dur > adur:
        start = max(0.0, adur - dur)
    muxed = work / "reel.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-ss", f"{start:.2f}",
                    "-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                    "-af", f"afade=t=out:st={max(dur - 0.8, 0):.2f}:d=0.8", "-t", f"{dur:.2f}",
                    "-movflags", "+faststart", str(muxed)], check=True)
    thumb = work / "thumb.jpg"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", "1.0", "-i", str(muxed),
                    "-frames:v", "1", "-q:v", "2", str(thumb)], check=True)
    extra = cl.clip_music_extra_data(track, audio_asset_start_time=int(start * 1000),
                                     overlap_duration=int(dur * 1000), original_volume=0.0)
    extra.update({"clips_share_preview_to_feed": "1"})
    media = cl.clip_upload(muxed, full_caption(item), thumbnail=thumb, extra_data=extra)
    asyncio.run(ig_direct.save_direct_session(cl.get_settings(), username=HANDLE.lstrip("@"), user_id=str(cl.user_id)))
    log(f"music: {track.title} - {track.display_artist} (from {start:.1f}s)")
    for f in work.iterdir():
        f.unlink(missing_ok=True)
    work.rmdir()
    return str(media.pk or media.id), f"https://www.instagram.com/reel/{media.code}/"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--render")
    ap.add_argument("--render-all", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--post-next", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    plan = load_plan()
    if a.render:
        item = next(it for it in plan if it["id"] == a.render)
        print(render_video(item, force=a.force))
    elif a.render_all:
        for it in plan:
            render_video(it, force=a.force)
    elif a.post_next:
        try:
            post_next(dry_run=a.dry_run)
        except Exception as exc:
            log(f"ERROR post-next: {exc!r}")
            raise
    else:
        st = load_state()
        for it in plan:
            p = st["posted"].get(it["id"])
            print(f"{it['id']:28} {'POSTED ' + p['at'][:16] + ' ' + p['permalink'] if p else '-'}")


if __name__ == "__main__":
    main()
