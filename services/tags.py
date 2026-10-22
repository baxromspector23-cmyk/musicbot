"""Tag reading/writing with mutagen (blocking; call via asyncio.to_thread)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import mutagen
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, ID3, TALB, TIT2, TPE1, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4Cover

SUPPORTED_EXT = frozenset({"mp3", "m4a", "m4r", "mp4", "flac", "ogg", "oga", "opus"})
COVER_EXT = frozenset({"mp3", "m4a", "m4r", "mp4", "flac"})


@dataclass
class Tags:
    """Basic tag set."""

    title: str = ""
    artist: str = ""
    album: str = ""

    def copy(self) -> "Tags":
        return Tags(self.title, self.artist, self.album)

    @property
    def is_empty(self) -> bool:
        return not (self.title or self.artist or self.album)


def tags_supported(ext: str) -> bool:
    """Whether the extension can hold tags."""
    return ext.lower().lstrip(".") in SUPPORTED_EXT


def cover_supported(ext: str) -> bool:
    """Whether the extension can hold a cover image."""
    return ext.lower().lstrip(".") in COVER_EXT


def read_tags(path: Path) -> Tags:
    """Read title/artist/album."""
    f = mutagen.File(path, easy=True)
    if f is None or f.tags is None:
        return Tags()

    def first(key: str) -> str:
        v = f.tags.get(key)
        return str(v[0]) if v else ""

    return Tags(first("title"), first("artist"), first("album"))


def read_cover(path: Path) -> bytes | None:
    """Read embedded cover bytes if present."""
    ext = path.suffix.lower().lstrip(".")
    try:
        if ext == "mp3":
            id3 = ID3(path)
            frames = id3.getall("APIC")
            return bytes(frames[0].data) if frames else None
        if ext in ("m4a", "m4r", "mp4"):
            covr = (MP4(path).tags or {}).get("covr")
            return bytes(covr[0]) if covr else None
        if ext == "flac":
            pics = FLAC(path).pictures
            return bytes(pics[0].data) if pics else None
    except Exception:
        return None
    return None


def write_tags(path: Path, tags: Tags, cover: bytes | None) -> None:
    """Write tags and cover into the file in place."""
    ext = path.suffix.lower().lstrip(".")
    if ext == "mp3":
        _write_mp3(path, tags, cover)
    elif ext in ("m4a", "m4r", "mp4"):
        _write_mp4(path, tags, cover)
    elif ext == "flac":
        _write_flac(path, tags, cover)
    elif ext in ("ogg", "oga", "opus"):
        _write_vorbis(path, tags)
    else:
        raise ValueError(f"tags not supported for .{ext}")


def _write_mp3(path: Path, tags: Tags, cover: bytes | None) -> None:
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        id3 = ID3()
    for key in ("TIT2", "TPE1", "TALB", "APIC"):
        id3.delall(key)
    # encoding=1 is UTF-16: displays Cyrillic and Uzbek letters correctly in ID3v2.3
    if tags.title:
        id3.add(TIT2(encoding=1, text=tags.title))
    if tags.artist:
        id3.add(TPE1(encoding=1, text=tags.artist))
    if tags.album:
        id3.add(TALB(encoding=1, text=tags.album))
    if cover:
        id3.add(APIC(encoding=1, mime="image/jpeg", type=3, desc="Cover", data=cover))
    id3.save(path, v2_version=3)


def _write_mp4(path: Path, tags: Tags, cover: bytes | None) -> None:
    mp4 = MP4(path)
    if mp4.tags is None:
        mp4.add_tags()
    t = mp4.tags
    for key, value in (("\xa9nam", tags.title), ("\xa9ART", tags.artist), ("\xa9alb", tags.album)):
        if value:
            t[key] = [value]
        elif key in t:
            del t[key]
    if cover:
        t["covr"] = [MP4Cover(cover, imageformat=MP4Cover.FORMAT_JPEG)]
    elif "covr" in t:
        del t["covr"]
    mp4.save()


def _write_flac(path: Path, tags: Tags, cover: bytes | None) -> None:
    f = FLAC(path)
    for key, value in (("title", tags.title), ("artist", tags.artist), ("album", tags.album)):
        if value:
            f[key] = [value]
        elif key in f:
            del f[key]
    f.clear_pictures()
    if cover:
        pic = Picture()
        pic.type = 3
        pic.mime = "image/jpeg"
        pic.desc = "Cover"
        pic.data = cover
        f.add_picture(pic)
    f.save()


def _write_vorbis(path: Path, tags: Tags) -> None:
    f = mutagen.File(path)
    if f is None:
        raise ValueError("unreadable ogg")
    if f.tags is None:
        f.add_tags()
    for key, value in (("title", tags.title), ("artist", tags.artist), ("album", tags.album)):
        if value:
            f[key] = [value]
        elif key in f:
            del f[key]
    f.save()
