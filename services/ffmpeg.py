"""FFmpeg wrapper: async subprocess runner and one function per operation."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import config
from services.probe import MediaInfo, ProbeError, probe  # noqa: F401  (probe re-exported)

log = logging.getLogger(__name__)

ProgressCb = Callable[[float], Awaitable[None]]


class FFmpegError(Exception):
    """FFmpeg exited with an error (message = last 500 chars of stderr)."""


class FFmpegTimeout(FFmpegError):
    """FFmpeg exceeded FFMPEG_TIMEOUT_SEC."""


class FFmpegCancelled(Exception):
    """The job was cancelled by the user."""


class InvalidInput(Exception):
    """Parameters are invalid; message is safe to show to the user."""


@dataclass
class Job:
    """Handle for one running operation (progress callback + cancellation)."""

    on_progress: ProgressCb | None = None
    proc: asyncio.subprocess.Process | None = None
    cancelled: bool = False

    def cancel(self) -> None:
        """Kill the running FFmpeg process."""
        self.cancelled = True
        if self.proc is not None and self.proc.returncode is None:
            try:
                self.proc.kill()
            except ProcessLookupError:
                pass


@dataclass
class Caps:
    """Detected FFmpeg capabilities."""

    filters: frozenset[str] = field(default_factory=frozenset)
    rubberband: bool = False
    libvorbis: bool = False
    libopus: bool = False


_CAPS: Caps | None = None


async def _capture(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", "ignore")


async def get_caps() -> Caps:
    """Detect (once) rubberband filter and libvorbis/libopus encoders."""
    global _CAPS
    if _CAPS is None:
        filters_out = await _capture("ffmpeg", "-hide_banner", "-filters")
        enc_out = await _capture("ffmpeg", "-hide_banner", "-encoders")
        names = frozenset(
            m.group(1) for m in re.finditer(r"^\s*[A-Z.]{3}\s+(\S+)\s", filters_out, re.M)
        )
        _CAPS = Caps(
            filters=names,
            rubberband="rubberband" in names,
            libvorbis=bool(re.search(r"\blibvorbis\b", enc_out)),
            libopus=bool(re.search(r"\blibopus\b", enc_out)),
        )
        log.info("ffmpeg caps: rubberband=%s libvorbis=%s libopus=%s",
                 _CAPS.rubberband, _CAPS.libvorbis, _CAPS.libopus)
    return _CAPS


async def has_filter(name: str) -> bool:
    """True when the ffmpeg build has the named filter."""
    return name in (await get_caps()).filters


async def ffmpeg_version() -> str:
    """First line of `ffmpeg -version`."""
    return (await _capture("ffmpeg", "-version")).splitlines()[0]


async def ffprobe_version() -> str:
    """First line of `ffprobe -version`."""
    return (await _capture("ffprobe", "-version")).splitlines()[0]


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def run_ffmpeg(
    args: list[str],
    expected_duration: float = 0.0,
    on_progress: ProgressCb | None = None,
    timeout: float | None = None,
    job: Job | None = None,
    loglevel: str = "error",
) -> str:
    """Run ffmpeg with progress parsing; return stderr text."""
    timeout = timeout or config.FFMPEG_TIMEOUT_SEC
    if job is not None and job.cancelled:
        raise FFmpegCancelled()
    callback = on_progress or (job.on_progress if job else None)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", loglevel, "-nostdin", "-y", "-threads", "1",
           "-progress", "pipe:1", "-nostats", *[str(a) for a in args]]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    if job is not None:
        job.proc = proc
    err = bytearray()

    async def read_err() -> None:
        assert proc.stderr is not None
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            err.extend(chunk)
            if len(err) > 64000:
                del err[: len(err) - 32000]

    async def read_out() -> None:
        assert proc.stdout is not None
        last = 0.0
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", "ignore").strip()
            if not text.startswith(("out_time_us=", "out_time_ms=")):
                continue
            try:
                us = int(text.partition("=")[2])
            except ValueError:
                continue
            now = time.monotonic()
            if us < 0 or expected_duration <= 0 or callback is None:
                continue
            if now - last >= config.PROGRESS_INTERVAL_SEC:
                last = now
                try:
                    await callback(min(99.0, us / 1_000_000 / expected_duration * 100))
                except Exception:
                    log.debug("progress callback failed", exc_info=True)

    try:
        await asyncio.wait_for(asyncio.gather(read_out(), read_err(), proc.wait()), timeout)
    except asyncio.TimeoutError as exc:
        _kill(proc)
        await proc.wait()
        raise FFmpegTimeout("ffmpeg timeout") from exc
    except BaseException:
        _kill(proc)
        raise
    stderr = err.decode("utf-8", "ignore")
    if job is not None and job.cancelled:
        raise FFmpegCancelled()
    if proc.returncode != 0:
        tail = stderr[-500:]
        log.error("ffmpeg failed (rc=%s): %s", proc.returncode, tail)
        raise FFmpegError(tail)
    return stderr


async def _run_to(dst: Path, args: list[str], expected: float, job: Job | None,
                  loglevel: str = "error") -> None:
    """Run ffmpeg writing to dst; delete partial output on any failure."""
    try:
        await run_ffmpeg([*args, str(dst)], expected, job=job, loglevel=loglevel)
        if not dst.exists() or dst.stat().st_size == 0:
            raise FFmpegError("empty output")
    except BaseException:
        dst.unlink(missing_ok=True)
        raise


def default_bitrate(info: MediaInfo) -> int:
    """Source bitrate clamped to 128-320k; 192k when unknown."""
    if info.bit_rate <= 0:
        return config.DEFAULT_BITRATE_K
    return max(config.MIN_BITRATE_K, min(config.MAX_BITRATE_K, round(info.bit_rate / 1000)))


def encoder_args(ext: str, info: MediaInfo, caps: Caps, bitrate_k: int | None = None,
                 same_format: bool = True) -> list[str]:
    """Encoder arguments for an output extension."""
    ext = ext.lower().lstrip(".")
    br = bitrate_k or default_bitrate(info)
    if ext == "mp3":
        return ["-c:a", "libmp3lame", "-b:a", f"{br}k"]
    if ext in ("m4a", "m4r"):
        return ["-c:a", "aac", "-b:a", f"{br}k", "-movflags", "+faststart"]
    if ext in ("ogg", "oga", "opus"):
        if same_format and info.codec_name == "opus" and caps.libopus:
            return ["-c:a", "libopus", "-b:a", f"{config.OPUS_BITRATE_K}k"]
        effective_br = min(br, 192) if (info.channels == 1 and br > 192) else br
        if caps.libvorbis:
            return ["-c:a", "libvorbis", "-b:a", f"{effective_br}k"]
        if caps.libopus:
            return ["-c:a", "libopus", "-b:a", f"{effective_br}k"]
        raise FFmpegError("no ogg encoder available")
    if ext == "wav":
        return ["-c:a", "pcm_s16le"]
    if ext == "flac":
        return ["-c:a", "flac", "-compression_level", "5"]
    raise InvalidInput(f"unsupported format {ext}")


def _fmt_args(dst: Path) -> list[str]:
    return ["-f", "ipod"] if dst.suffix.lower() == ".m4r" else []


def _src(src: Path) -> list[str]:
    return ["-i", str(src), "-vn", "-map", "0:a:0", "-map_metadata", "-1"]


async def _filter_job(src: Path, dst: Path, info: MediaInfo, af: str, expected: float,
                      job: Job | None, bitrate_k: int | None = None, same_format: bool = True) -> None:
    caps = await get_caps()
    args = [*_src(src), "-af", af, *encoder_args(dst.suffix, info, caps, bitrate_k, same_format), *_fmt_args(dst)]
    await _run_to(dst, args, expected, job)


# ---------------------------------------------------------------- operations

async def cut(src: Path, dst: Path, info: MediaInfo, start: float, end: float,
              job: Job | None = None) -> None:
    """Cut [start, end]; stream copy for mp3/m4a/flac/wav, retry with re-encode on failure."""
    length = end - start
    pre = ["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
           "-vn", "-map", "0:a:0", "-map_metadata", "-1"]
    dst_ext = dst.suffix.lower().lstrip(".")
    if dst_ext in ("mp3", "m4a", "flac", "wav") and (dst_ext == info.native_ext or (dst_ext == "mp3" and info.codec_name == "mp3")):
        try:
            await _run_to(dst, [*pre, "-c:a", "copy"], length, job)
            cut_info = await probe(dst)
            if abs(cut_info.duration - length) <= 1.0:
                return
            log.warning("stream copy cut produced inaccurate duration %.2f (expected %.2f), re-encoding",
                        cut_info.duration, length)
        except FFmpegError as exc:
            if isinstance(exc, FFmpegTimeout):
                raise
            log.warning("stream copy cut failed, re-encoding")
    caps = await get_caps()
    await _run_to(dst, [*pre, *encoder_args(dst_ext, info, caps), *_fmt_args(dst)], length, job)


async def fade(src: Path, dst: Path, info: MediaInfo, mode: str, d: float,
               job: Job | None = None) -> None:
    """Apply fade in/out/both of d seconds."""
    parts: list[str] = []
    if mode in ("in", "both"):
        parts.append(f"afade=t=in:st=0:d={d:.3f}")
    if mode in ("out", "both"):
        parts.append(f"afade=t=out:st={max(info.duration - d, 0):.3f}:d={d:.3f}")
    await _filter_job(src, dst, info, ",".join(parts), info.duration, job)


async def change_speed(src: Path, dst: Path, info: MediaInfo, factor: float, keep_pitch: bool,
                       job: Job | None = None) -> None:
    """Change speed with (atempo) or without (asetrate) keeping pitch."""
    sr = info.sample_rate
    af = f"atempo={factor:.4f}" if keep_pitch else f"asetrate={sr * factor:.2f},aresample={sr}"
    await _filter_job(src, dst, info, af, info.duration / factor, job)


async def shift_pitch(src: Path, dst: Path, info: MediaInfo, semitones: int, job: Job | None = None,
                      use_rubberband: bool | None = None) -> None:
    """Shift pitch by semitones keeping duration."""
    f = 2 ** (semitones / 12)
    sr = info.sample_rate
    if use_rubberband is None:
        use_rubberband = await has_filter("rubberband")
    if use_rubberband:
        af = f"rubberband=pitch={f:.6f}"
    else:
        af = f"asetrate={sr * f:.2f},aresample={sr},atempo={1 / f:.6f}"
    await _filter_job(src, dst, info, af, info.duration, job)


async def change_volume(src: Path, dst: Path, info: MediaInfo, factor: float,
                        job: Job | None = None) -> None:
    """Scale volume with a limiter against clipping."""
    await _filter_job(src, dst, info, f"volume={factor:.4f},alimiter=limit=0.97", info.duration, job)


def _parse_loudnorm(stderr: str) -> dict[str, float] | None:
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr)
    if not blocks:
        return None
    try:
        raw = json.loads(blocks[-1])
        keys = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        return {k: float(raw[k]) for k in keys}
    except (ValueError, KeyError):
        return None


async def normalize(src: Path, dst: Path, info: MediaInfo, target_lufs: float,
                    job: Job | None = None) -> None:
    """Loudness normalization (two-pass; single-pass for long files)."""
    base = f"loudnorm=I={target_lufs}:TP=-1.5:LRA=11"
    af = base
    expected = info.duration
    if info.duration <= config.NORMALIZE_SINGLE_PASS_SEC:
        stderr = await run_ffmpeg(
            ["-i", str(src), "-vn", "-map", "0:a:0", "-af", f"{base}:print_format=json", "-f", "null", "-"],
            info.duration / 2, job=job, loglevel="info",
        )
        m = _parse_loudnorm(stderr)
        if m is not None:
            af = (f"{base}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
                  f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
                  f"offset={m['target_offset']}:linear=true")
        else:
            log.warning("loudnorm measurement unavailable, using single pass")
    caps = await get_caps()
    args = [*_src(src), "-af", af, "-ar", str(info.sample_rate),
            *encoder_args(dst.suffix, info, caps), *_fmt_args(dst)]
    await _run_to(dst, args, expected, job)


async def convert(src: Path, dst: Path, info: MediaInfo, bitrate_k: int | None,
                  job: Job | None = None) -> None:
    """Convert to the format given by dst's extension."""
    caps = await get_caps()
    args = [*_src(src), *encoder_args(dst.suffix, info, caps, bitrate_k, same_format=False), *_fmt_args(dst)]
    await _run_to(dst, args, info.duration, job)


async def merge(items: list[tuple[Path, MediaInfo]], dst: Path, crossfade: bool,
                job: Job | None = None) -> None:
    """Merge files (plain concat or acrossfade) into an mp3 192k."""
    n = len(items)
    if n < 2:
        raise InvalidInput("need at least two files")
    fade_len = config.CROSSFADE_SEC
    if crossfade and any(i.duration < 2 * fade_len for _, i in items):
        raise InvalidInput(f"crossfade needs files of at least {2 * fade_len}s")
    args: list[str] = []
    for p, _ in items:
        args += ["-i", str(p)]
    parts = [f"[{i}:a:0]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
             for i in range(n)]
    if crossfade:
        prev = "[a0]"
        for i in range(1, n):
            out = "[out]" if i == n - 1 else f"[x{i}]"
            parts.append(f"{prev}[a{i}]acrossfade=d={fade_len}{out}")
            prev = out
        total = sum(i.duration for _, i in items) - fade_len * (n - 1)
    else:
        parts.append("".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]")
        total = sum(i.duration for _, i in items)
    args += ["-filter_complex", ";".join(parts), "-map", "[out]", "-map_metadata", "-1",
             "-c:a", "libmp3lame", "-b:a", "192k"]
    await _run_to(dst, args, total, job)


def extract_target_ext(info: MediaInfo, mode: str) -> str:
    """Output extension for extract_audio."""
    if mode == "orig" and info.codec_name == "aac":
        return "m4a"
    return "mp3"


async def extract_audio(src: Path, dst: Path, info: MediaInfo, mode: str, job: Job | None = None) -> None:
    """Extract the audio track (stream copy for aac/mp3 when mode == 'orig')."""
    base = ["-i", str(src), "-vn", "-map", "0:a:0", "-map_metadata", "-1"]
    if mode == "orig" and info.codec_name == "aac":
        await _run_to(dst, [*base, "-c:a", "copy", "-movflags", "+faststart"], info.duration, job)
    elif mode == "orig" and info.codec_name == "mp3":
        await _run_to(dst, [*base, "-c:a", "copy"], info.duration, job)
    else:
        br = 320 if mode == "mp3_320" else 192
        await _run_to(dst, [*base, "-c:a", "libmp3lame", "-b:a", f"{br}k"], info.duration, job)


async def make_ringtone(src: Path, dst: Path, info: MediaInfo, start: float, length: float,
                        fade_out: float, job: Job | None = None) -> None:
    """Cut a ringtone with fades; mp3 192k (Android) or AAC .m4r (iPhone)."""
    af = f"afade=t=in:st=0:d=0.3,afade=t=out:st={max(length - fade_out, 0):.3f}:d={fade_out:.3f}"
    caps = await get_caps()
    args = ["-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}", "-vn", "-map", "0:a:0",
            "-map_metadata", "-1", "-af", af, *encoder_args(dst.suffix, info, caps, 192), *_fmt_args(dst)]
    await _run_to(dst, args, length, job)


async def make_cover(src_img: Path, dst: Path, job: Job | None = None) -> None:
    """Convert any image to a centered square JPEG."""
    vf = f"crop='min(iw,ih)':'min(iw,ih)',scale={config.COVER_PX}:{config.COVER_PX}"
    await _run_to(dst, ["-i", str(src_img), "-vf", vf, "-frames:v", "1", "-q:v", "3"], 0, job)


async def extract_cover(src: Path, dst: Path) -> None:
    """Extract an embedded cover as JPEG."""
    await _run_to(dst, ["-i", str(src), "-an", "-map", "0:v:0", "-frames:v", "1", "-q:v", "3"], 0, None)


async def make_thumbnail(cover: Path, dst: Path) -> bool:
    """Create a <=320px, <200KB JPEG thumbnail; False if it could not be made."""
    px = config.THUMB_MAX_PX
    vf = f"scale={px}:{px}:force_original_aspect_ratio=decrease"
    for q in (4, 8, 14, 24):
        try:
            await _run_to(dst, ["-i", str(cover), "-vf", vf, "-frames:v", "1", "-q:v", str(q)], 0, None)
        except FFmpegError:
            return False
        if dst.stat().st_size <= config.THUMB_MAX_KB * 1024:
            return True
    dst.unlink(missing_ok=True)
    return False
