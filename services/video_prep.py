"""Video validation, aspect ratio normalization, bitrate capping, and frame extraction for Instagram Reels."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import config
import texts
from services import ffmpeg
from services.probe import MediaInfo, probe
from services.session import JOB_SEM
from utils import UserError, fmt_duration

log = logging.getLogger(__name__)

TARGET_ASPECT = 9.0 / 16.0  # 0.5625
ASPECT_TOLERANCE = 0.05
MIN_REEL_DURATION = 3.0
MAX_REEL_DURATION = 15 * 60.0  # 15 minutes


@dataclass
class VideoSpecs:
    width: int
    height: int
    duration: float
    fps: float
    is_9_16: bool
    codec_name: str
    size: int


def is_aspect_9_16(width: int, height: int) -> bool:
    """True if dimensions are close to 9:16 aspect ratio."""
    if height <= 0 or width <= 0:
        return False
    ratio = width / height
    return abs(ratio - TARGET_ASPECT) <= ASPECT_TOLERANCE


async def check_video_specs(path: Path) -> tuple[MediaInfo, VideoSpecs]:
    """Validate video using ffprobe; raise UserError if unsupported or invalid for Reels."""
    info = await probe(path)
    if not info.has_video:
        raise UserError(texts.REEL_NO_VIDEO)
    if info.duration < MIN_REEL_DURATION:
        raise UserError(texts.REEL_TOO_SHORT)
    if info.duration > MAX_REEL_DURATION:
        raise UserError(texts.REEL_TOO_LONG)

    # Fetch exact width, height, fps with ffprobe
    import json
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,codec_name",
        "-print_format", "json", str(path),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, _ = await proc.communicate()
    width, height = 0, 0
    fps = 30.0
    codec = info.codec_name
    try:
        data = json.loads(out.decode("utf-8", "ignore"))
        streams = data.get("streams", [])
        if streams:
            vstream = streams[0]
            width = int(vstream.get("width") or 0)
            height = int(vstream.get("height") or 0)
            codec = str(vstream.get("codec_name") or codec)
            r_fps = str(vstream.get("r_frame_rate", "30/1"))
            if "/" in r_fps:
                num, den = r_fps.split("/")
                fps = float(num) / float(den) if float(den) != 0 else 30.0
    except Exception as exc:
        log.warning("could not parse video stream JSON: %s", exc)

    specs = VideoSpecs(
        width=width or 1080,
        height=height or 1920,
        duration=info.duration,
        fps=fps,
        is_9_16=is_aspect_9_16(width, height),
        codec_name=codec,
        size=path.stat().st_size,
    )
    return info, specs


async def prepare_reel_video(
    src: Path,
    dst: Path,
    info: MediaInfo,
    specs: VideoSpecs,
    aspect_mode: str = "crop",  # "crop", "pad", or "original"
    on_progress: ffmpeg.ProgressCb | None = None,
) -> None:
    """Normalize video to H.264/AAC MP4, apply 9:16 transformation, cap bitrate to MAX_REEL_MB."""
    duration = specs.duration
    target_bytes = config.MAX_REEL_MB * 1024 * 1024 * 0.95
    target_total_bits = target_bytes * 8
    audio_bitrate = 128_000
    available_video_bits = target_total_bits - (audio_bitrate * duration)
    target_video_bitrate = int(available_video_bits / duration)

    if target_video_bitrate < 250_000:
        raise UserError(texts.REEL_VIDEO_QUALITY_LOW.format(duration=fmt_duration(duration)))

    max_v_rate = min(target_video_bitrate, 3_500_000)
    buf_size = max_v_rate * 2

    # Build video filter & map arguments
    if aspect_mode == "crop":
        # Scale to fill 1080x1920 keeping aspect ratio, then crop excess from center to exact 1080x1920
        vf = (
            "scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920:(iw-1080)/2:(ih-1920)/2"
        )
        filter_args = ["-vf", vf]
        video_map = ["0:v:0"]
    elif aspect_mode == "pad":
        # Pad with blurred background filling 1080x1920, with labeled [v] output to prevent duplicate streams
        vf = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,boxblur=20:5[bg];"
            "[0:v]scale=1080:1920:force_original_aspect_ratio=decrease[fg];"
            "[bg][fg]overlay=(W-w)/2:(H-h)/2[v]"
        )
        filter_args = ["-filter_complex", vf]
        video_map = ["[v]"]
    else:  # "original"
        vf = (
            "scale=w='min(1080,iw)':h='min(1920,ih)':force_original_aspect_ratio=decrease,"
            "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        )
        filter_args = ["-vf", vf]
        video_map = ["0:v:0"]

    input_args = ["-i", str(src)]
    if not info.has_audio:
        input_args.extend(["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])

    map_args = ["-map", *video_map]
    if info.has_audio:
        map_args.extend(["-map", "0:a:0"])
    else:
        map_args.extend(["-map", "1:a:0"])

    cmd_args = [
        *input_args,
        *filter_args,
        *map_args,
        "-c:v", "libx264",
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-preset", "veryfast",
        "-crf", "23",
        "-maxrate", f"{max_v_rate}",
        "-bufsize", f"{buf_size}",
        "-r", "30",
        "-movflags", "+faststart",
        "-threads", "2",
        "-c:a", "aac",
        "-b:a", "128k",
        "-ac", "2",
        "-ar", "44100",
        *(["-shortest"] if not info.has_audio else []),
    ]

    async with JOB_SEM:
        await ffmpeg.run_ffmpeg([*cmd_args, str(dst)], expected_duration=duration, on_progress=on_progress)

    if not dst.exists() or dst.stat().st_size == 0:
        raise ffmpeg.FFmpegError("video preparation produced empty file")


async def extract_preview_frames(video_path: Path, duration: float) -> list[bytes]:
    """Extract 3 representative frames (scaled to 512px JPEG) for LLM vision input."""
    frames: list[bytes] = []
    points = [0.20 * duration, 0.50 * duration, 0.80 * duration]
    temp_dir = video_path.parent / "frames"
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        for idx, pt in enumerate(points):
            frame_path = temp_dir / f"f_{idx}.jpg"
            vf = "scale=512:512:force_original_aspect_ratio=decrease"
            cmd = [
                "-ss", f"{pt:.2f}",
                "-i", str(video_path),
                "-vf", vf,
                "-frames:v", "1",
                "-q:v", "3",
            ]
            try:
                await ffmpeg.run_ffmpeg([*cmd, str(frame_path)])
                if frame_path.exists() and frame_path.stat().st_size > 0:
                    frames.append(await asyncio.to_thread(frame_path.read_bytes))
            except Exception:
                log.warning("failed to extract frame at %.2f seconds", pt)
            finally:
                frame_path.unlink(missing_ok=True)
    finally:
        try:
            temp_dir.rmdir()
        except OSError:
            pass

    return frames
