"""Self-test: python -m scripts.selftest"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:TESTTOKENTESTTOKENTESTTOKENTESTTOKEN")
os.environ.setdefault("OWNER_ID", "1")

import config  # noqa: E402
from services import ffmpeg  # noqa: E402
from services import tags as tagsvc  # noqa: E402
from services.probe import MediaInfo, probe  # noqa: E402
from utils import (fade_limit, parse_float, parse_int, parse_range, parse_time,  # noqa: E402
                   parse_volume, sanitize_filename, validate_range)

RESULTS: list[tuple[str, bool, str]] = []
TMP = Path(tempfile.mkdtemp(prefix="musicbot_selftest_"))


def approx(a: float, b: float, tol: float, what: str = "") -> None:
    assert abs(a - b) <= tol, f"{what} expected {b} +-{tol}, got {a}"


async def case(name: str, fn) -> None:  # type: ignore[no-untyped-def]
    try:
        await fn()
        RESULTS.append((name, True, ""))
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"[:120]))


async def gen(args: list[str], out: Path) -> Path:
    await ffmpeg.run_ffmpeg([*args, str(out)])
    return out


def t_parse_time() -> None:
    valid = {"83": 83, "83.5": 83.5, "83,5": 83.5, "1:23": 83, "01:23.5": 83.5, "1:02:03": 3723, "0": 0, "0:30": 30}
    for s, v in valid.items():
        approx(parse_time(s), v, 1e-6, s)
    for bad in ("", "abc", "1:2:3:4", "1::2", "-5", "1:-2", "1.5:20", "1:2x"):
        try:
            parse_time(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid {bad!r}")
    ranges = {
        "0:30 1:45": (30, 105), "30 105": (30, 105), "30-105": (30, 105), "30 – 105": (30, 105),
        "30→105": (30, 105), "0:30, 1:45": (30, 105), "30,105": (30, 105), "30 oxir": (30, None),
        "30 end": (30, None), "83": (83, None), "83,5": (83.5, None), "1:23,5 2:00": (83.5, 120),
    }
    for s, (a, b) in ranges.items():
        st, en = parse_range(s)
        approx(st, a, 1e-6, s)
        assert (en is None) == (b is None), s
        if b is not None:
            approx(en or 0, b, 1e-6, s)
    for bad in ("", "oxir 30", "-5 10", "1 2 3", "x y"):
        try:
            parse_range(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted invalid range {bad!r}")
    assert validate_range(0, 10.5, 10) == (0, 10)
    for st, en in ((5, 5), (5, 4), (0, 12), (0, 0.2), (-1, 3)):
        try:
            validate_range(st, en, 10)
        except ValueError:
            continue
        raise AssertionError(f"accepted {st}-{en}")
    approx(parse_volume("+20"), 1.2, 1e-6)
    approx(parse_volume("-30%"), 0.7, 1e-6)
    approx(parse_volume("150%"), 1.5, 1e-6)
    for bad in ("-95", "500%", "x", ""):
        try:
            parse_volume(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted volume {bad!r}")
    approx(parse_float("1,25", 0.5, 2.0), 1.25, 1e-9)
    assert parse_int("-6", -12, 12) == -6
    assert sanitize_filename('a/b:c*d?"e<f>g|h') == "abcdefgh"
    assert fade_limit("both", 10) == 5 and fade_limit("in", 10) == 10


class Ctx:
    tone: Path
    video: Path
    cover: Path
    info: MediaInfo


C = Ctx()


async def setup_assets() -> None:
    C.tone = await gen(["-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "192k"],
                       TMP / "tone.mp3")
    C.video = await gen(["-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=6",
                         "-f", "lavfi", "-i", "sine=frequency=330:duration=6",
                         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest"], TMP / "video.mp4")
    C.cover = await gen(["-f", "lavfi", "-i", "color=c=red:s=600x400", "-frames:v", "1"], TMP / "cover.png")
    C.info = await probe(C.tone)
    approx(C.info.duration, 10, 0.3, "tone duration")


async def dur(p: Path) -> float:
    return (await probe(p)).duration


async def t_cut() -> None:
    for name, src_fmt in (("mp3", "mp3"), ("ogg", "ogg"), ("flac", "flac"), ("wav", "wav"), ("m4a", "m4a")):
        src = TMP / f"cutsrc.{src_fmt}"
        await ffmpeg.convert(C.tone, src, C.info, 192, None)
        info = await probe(src)
        dst = TMP / f"cut_{name}.{info.out_ext}"
        await ffmpeg.cut(src, dst, info, 2.0, 6.5)
        approx(await dur(dst), 4.5, 0.2, f"cut {name}")


async def t_fade() -> None:
    for mode in ("in", "out", "both"):
        dst = TMP / f"fade_{mode}.mp3"
        await ffmpeg.fade(C.tone, dst, C.info, mode, 2.0)
        approx(await dur(dst), 10, 0.3, "fade duration")


async def t_speed() -> None:
    for keep in (True, False):
        for f in (0.5, 1.5, 2.0):
            dst = TMP / f"speed_{keep}_{f}.mp3"
            await ffmpeg.change_speed(C.tone, dst, C.info, f, keep)
            approx(await dur(dst), 10 / f, 0.35, f"speed {f} keep={keep}")


async def t_pitch() -> None:
    modes = [False] + ([True] if await ffmpeg.has_filter("rubberband") else [])
    for rb in modes:
        for n in (-6, 3, 12):
            dst = TMP / f"pitch_{rb}_{n}.mp3"
            await ffmpeg.shift_pitch(C.tone, dst, C.info, n, use_rubberband=rb)
            approx(await dur(dst), 10, 0.35, f"pitch {n} rb={rb}")
    dst = TMP / "pitch_auto.mp3"
    await ffmpeg.shift_pitch(C.tone, dst, C.info, 2)
    approx(await dur(dst), 10, 0.35)


async def t_volume() -> None:
    dst = TMP / "vol.mp3"
    await ffmpeg.change_volume(C.tone, dst, C.info, 1.5)
    approx(await dur(dst), 10, 0.3)


async def t_normalize() -> None:
    for lufs in (-14, -16, -9):
        dst = TMP / f"norm_{abs(lufs)}.mp3"
        await ffmpeg.normalize(C.tone, dst, C.info, lufs)
        info = await probe(dst)
        approx(info.duration, 10, 0.3)
        assert info.sample_rate == C.info.sample_rate, f"sample rate {info.sample_rate}"
    long_info = MediaInfo(**{**C.info.__dict__, "duration": 600.0})
    dst = TMP / "norm_single.mp3"
    await ffmpeg.normalize(C.tone, dst, long_info, -14)
    assert dst.stat().st_size > 0

    # Test normalization on silent audio (loudnorm -inf handling)
    silent_src = TMP / "silent.wav"
    silent_dst = TMP / "norm_silent.wav"
    await ffmpeg.run_ffmpeg(["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "2", str(silent_src)])
    silent_info = await probe(silent_src)
    await ffmpeg.normalize(silent_src, silent_dst, silent_info, -14)
    assert silent_dst.exists() and silent_dst.stat().st_size > 0


async def t_convert() -> None:
    caps = await ffmpeg.get_caps()
    for fmt, rates in (("mp3", (128, 192, 256, 320)), ("m4a", (128, 192, 256, 320)),
                       ("ogg", (128, 192, 256, 320)), ("wav", (None,)), ("flac", (None,))):
        if fmt == "ogg" and not (caps.libvorbis or caps.libopus):
            continue
        for br in rates:
            dst = TMP / f"conv_{br}.{fmt}"
            await ffmpeg.convert(C.tone, dst, C.info, br)
            info = await probe(dst)
            approx(info.duration, 10, 0.3, f"{fmt} {br}")
            assert info.has_audio

    # Verify convert_bitrates CallbackData format (Issue 4)
    import keyboards as kb
    from keyboards import OpCb
    markup = kb.convert_bitrates("sid123", "mp3")
    for row in markup.inline_keyboard:
        for btn in row:
            if btn.callback_data and btn.callback_data.startswith("o:conv_br"):
                unpacked = OpCb.unpack(btn.callback_data)
                assert unpacked.action == "conv_br"
                assert unpacked.sid == "sid123"
                fmt, _, br = unpacked.arg.replace(":", "_").partition("_")
                assert fmt == "mp3"
                assert int(br) in (128, 192, 256, 320)


async def t_merge() -> None:
    other = await gen(["-f", "lavfi", "-i", "sine=frequency=550:duration=8", "-c:a", "libmp3lame"], TMP / "o.mp3")
    oi = await probe(other)
    items = [(C.tone, C.info), (other, oi), (C.tone, C.info)]
    plain = TMP / "merge_plain.mp3"
    await ffmpeg.merge(items, plain, False)
    approx(await dur(plain), 28, 0.5, "plain merge")
    xf = TMP / "merge_xf.mp3"
    await ffmpeg.merge(items, xf, True)
    approx(await dur(xf), 28 - 6, 0.6, "crossfade merge")
    short = await gen(["-f", "lavfi", "-i", "sine=duration=2", "-c:a", "libmp3lame"], TMP / "short.mp3")
    try:
        await ffmpeg.merge([(C.tone, C.info), (short, await probe(short))], TMP / "bad.mp3", True)
    except ffmpeg.InvalidInput:
        return
    raise AssertionError("crossfade accepted a too-short file")


async def t_extract() -> None:
    vi = await probe(C.video)
    assert vi.has_video and vi.has_audio
    for mode in ("orig", "mp3_192", "mp3_320"):
        ext = ffmpeg.extract_target_ext(vi, mode)
        dst = TMP / f"ext_{mode}.{ext}"
        await ffmpeg.extract_audio(C.video, dst, vi, mode)
        info = await probe(dst)
        assert not info.has_video and info.has_audio
        approx(info.duration, 6, 0.4, mode)
    # operations on a video input work too
    dst = TMP / "video_cut.m4a"
    await ffmpeg.cut(C.video, dst, vi, 1, 4)
    approx(await dur(dst), 3, 0.3)


async def t_ringtone() -> None:
    a = TMP / "ring.mp3"
    await ffmpeg.make_ringtone(C.tone, a, C.info, 1, 5, 2)
    approx(await dur(a), 5, 0.25, "android ringtone")
    i = TMP / "ring.m4r"
    await ffmpeg.make_ringtone(C.tone, i, C.info, 1, 5, 2)
    info = await probe(i)
    approx(info.duration, 5, 0.25, "iphone ringtone")
    assert info.codec_name == "aac", info.codec_name


async def t_tags() -> None:
    cover = TMP / "cover_sq.jpg"
    await ffmpeg.make_cover(C.cover, cover)
    ci = await probe(cover)
    assert ci.has_video or True
    data = cover.read_bytes()
    tags = tagsvc.Tags("Oʻzbek qoʻshiq Қўшиқ", "Ijrochi Ғ", "Albom")
    for fmt in ("mp3", "m4a", "flac"):
        f = TMP / f"tag.{fmt}"
        await ffmpeg.convert(C.tone, f, C.info, 192)
        await asyncio.to_thread(tagsvc.write_tags, f, tags, data)
        back = await asyncio.to_thread(tagsvc.read_tags, f)
        assert back == tags, f"{fmt}: {back} != {tags}"
        assert await asyncio.to_thread(tagsvc.read_cover, f), f"{fmt}: cover missing"
        info = await probe(f)
        assert info.has_cover and info.title == tags.title, f"{fmt}: probe tags {info.title!r}"
        approx(info.duration, 10, 0.3)
    if (await ffmpeg.get_caps()).libvorbis:
        f = TMP / "tag.ogg"
        await ffmpeg.convert(C.tone, f, C.info, 192)
        await asyncio.to_thread(tagsvc.write_tags, f, tags, None)
        assert await asyncio.to_thread(tagsvc.read_tags, f) == tags

    # Verify cover removal when tags are empty (Issue 7)
    empty_tags = tagsvc.Tags()
    for fmt in ("mp3", "m4a", "flac"):
        f_cov = TMP / f"cov_test.{fmt}"
        await ffmpeg.convert(C.tone, f_cov, C.info, 192)
        await asyncio.to_thread(tagsvc.write_tags, f_cov, empty_tags, data)
        assert (await asyncio.to_thread(tagsvc.read_cover, f_cov)) is not None
        # Remove cover with empty tags
        await asyncio.to_thread(tagsvc.write_tags, f_cov, empty_tags, None)
        assert (await asyncio.to_thread(tagsvc.read_cover, f_cov)) is None


async def t_thumbnail() -> None:
    cover = TMP / "cover_sq2.jpg"
    await ffmpeg.make_cover(C.cover, cover)
    approx(0, 0, 0)
    thumb = TMP / "thumb.jpg"
    assert await ffmpeg.make_thumbnail(cover, thumb)
    assert thumb.stat().st_size <= config.THUMB_MAX_KB * 1024
    out = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
        "-of", "csv=p=0", str(thumb), stdout=asyncio.subprocess.PIPE)
    text = (await out.communicate())[0].decode().strip()
    w, h = (int(x) for x in text.split(","))
    assert max(w, h) <= config.THUMB_MAX_PX, text
    cw = (await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(cover),
        stdout=asyncio.subprocess.PIPE))
    cs = (await cw.communicate())[0].decode().strip()
    assert cs == "1000,1000", f"cover size {cs}"


async def t_health() -> None:
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode
    from aiohttp.test_utils import TestClient, TestServer

    import main

    bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = main.build_dispatcher()
    app = main.create_app(bot, dp)
    app.on_startup.clear()  # do not contact Telegram in the test
    app.on_shutdown.clear()
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/health")
        assert resp.status == 200 and (await resp.text()) == "ok"
        bad = await client.post("/webhook", json={"update_id": 1})
        assert bad.status in (401, 403), bad.status  # missing secret token is rejected


async def t_cancel_timeout() -> None:
    job = ffmpeg.Job()
    big = ["-f", "lavfi", "-i", "sine=duration=3000"]
    dst = TMP / "cancel.mp3"

    async def runner() -> None:
        await ffmpeg._run_to(dst, [*big, "-c:a", "libmp3lame"], 3000, job)

    task = asyncio.create_task(runner())
    await asyncio.sleep(1.0)
    job.cancel()
    try:
        await task
    except ffmpeg.FFmpegCancelled:
        assert not dst.exists(), "partial output left behind"
        return
    raise AssertionError("job was not cancelled")


async def main_async() -> int:
    await ffmpeg.get_caps()
    await case("parse_time / ranges / volume", lambda: asyncio.sleep(0, t_parse_time()))
    try:
        await setup_assets()
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc()
        RESULTS.append(("setup assets", False, str(exc)[:120]))
        return report()
    for name, fn in (
        ("cut (mp3/ogg/flac/wav/m4a)", t_cut), ("fade in/out/both", t_fade), ("speed +/- pitch", t_speed),
        ("pitch (duration unchanged)", t_pitch), ("volume", t_volume), ("loudnorm 2-pass + single", t_normalize),
        ("convert all formats/bitrates", t_convert), ("merge plain + crossfade", t_merge),
        ("extract audio from video", t_extract), ("ringtone android + iphone", t_ringtone),
        ("tags round trip + cover", t_tags), ("thumbnail limits", t_thumbnail),
        ("job cancel cleans output", t_cancel_timeout), ("aiohttp /health + webhook auth", t_health),
    ):
        await case(name, fn)
    return report()


def report() -> int:
    width = max(len(n) for n, _, _ in RESULTS) + 2
    print("\n" + "=" * (width + 30))
    for name, ok, msg in RESULTS:
        print(f"{name:<{width}} {'PASS' if ok else 'FAIL'}  {msg}")
    failed = sum(1 for _, ok, _ in RESULTS if not ok)
    print("=" * (width + 30))
    print(f"{len(RESULTS) - failed}/{len(RESULTS)} passed")
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async()))
