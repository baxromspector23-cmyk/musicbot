"""Inline keyboards and CallbackData factories."""
from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import texts


class MenuCb(CallbackData, prefix="m"):
    """Main menu and navigation callbacks."""

    action: str
    sid: str
    arg: str = ""


class OpCb(CallbackData, prefix="o"):
    """Operation parameter callbacks."""

    action: str
    sid: str
    arg: str = ""


def _m(text: str, action: str, sid: str, arg: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=MenuCb(action=action, sid=sid, arg=arg).pack())


def _o(text: str, action: str, sid: str, arg: str = "") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=OpCb(action=action, sid=sid, arg=arg).pack())


def _kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*row)
    return b.as_markup()


def _chunks(buttons: list[InlineKeyboardButton], n: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + n] for i in range(0, len(buttons), n)]


def _back(sid: str) -> list[InlineKeyboardButton]:
    return [_m(texts.BTN_MENU, "menu", sid)]


def main_menu(sid: str, has_video: bool) -> InlineKeyboardMarkup:
    """Main menu."""
    rows = [
        [_m(texts.BTN_CUT, "cut", sid), _m(texts.BTN_FADE, "fade", sid)],
        [_m(texts.BTN_SPEED, "speed", sid), _m(texts.BTN_PITCH, "pitch", sid)],
        [_m(texts.BTN_VOLUME, "vol", sid), _m(texts.BTN_MERGE, "merge", sid)],
        [_m(texts.BTN_CONVERT, "conv", sid)],
    ]
    if has_video:
        rows[-1].append(_m(texts.BTN_EXTRACT, "ext", sid))
        rows.append([_m(texts.BTN_MENU_IG, "ig_reel", sid)])
    rows.append([_m(texts.BTN_TAGS, "tags", sid), _m(texts.BTN_RINGTONE, "ring", sid)])
    rows.append([_m(texts.BTN_UNDO, "undo", sid), _m(texts.BTN_DONE, "done", sid)])
    return _kb(rows)


def after_result(sid: str) -> InlineKeyboardMarkup:
    """Buttons shown after a finished operation."""
    return _kb([[_m(texts.BTN_AGAIN, "menu", sid)],
                [_m(texts.BTN_UNDO, "undo", sid), _m(texts.BTN_DONE, "done", sid)]])


def input_back(sid: str) -> InlineKeyboardMarkup:
    """Single 'back to menu' button."""
    return _kb([_back(sid)])


def job_cancel(sid: str) -> InlineKeyboardMarkup:
    """Cancel button under the progress message."""
    return _kb([[_o(texts.BTN_CANCEL, "jobcancel", sid)]])


def fade_modes(sid: str) -> InlineKeyboardMarkup:
    return _kb([[_o(texts.BTN_FADE_IN, "fade_mode", sid, "in"), _o(texts.BTN_FADE_OUT, "fade_mode", sid, "out")],
                [_o(texts.BTN_FADE_BOTH, "fade_mode", sid, "both")], _back(sid)])


def fade_seconds(sid: str) -> InlineKeyboardMarkup:
    btns = [_o(f"{v} s", "fade_sec", sid, str(v)) for v in (1, 2, 3, 5, 10)]
    return _kb([btns, _back(sid)])


def speed_kb(sid: str, keep_pitch: bool) -> InlineKeyboardMarkup:
    btns = [_o(f"{v}x", "speed_set", sid, str(v)) for v in ("0.5", "0.75", "1.25", "1.5", "2.0")]
    toggle = texts.BTN_KEEP_PITCH if keep_pitch else texts.BTN_CHANGE_PITCH
    return _kb([btns, [_o(toggle, "speed_toggle", sid)], _back(sid)])


def pitch_kb(sid: str) -> InlineKeyboardMarkup:
    vals = [-6, -4, -2, -1, 1, 2, 4, 6]
    btns = [_o(f"{v:+d}", "pitch_set", sid, str(v)) for v in vals]
    return _kb([*_chunks(btns, 4), _back(sid)])


def volume_kb(sid: str) -> InlineKeyboardMarkup:
    btns = [_o(v, "vol_set", sid, v) for v in ("-50%", "-20%", "+20%", "+50%")]
    return _kb([btns, [_o(texts.BTN_NORMALIZE, "norm_menu", sid)], _back(sid)])


def normalize_kb(sid: str) -> InlineKeyboardMarkup:
    return _kb([[_o(texts.BTN_NORM_STREAM, "norm_set", sid, "-14")],
                [_o(texts.BTN_NORM_16, "norm_set", sid, "-16")],
                [_o(texts.BTN_NORM_LOUD, "norm_set", sid, "-9")], _back(sid)])


def merge_kb(sid: str, n: int) -> InlineKeyboardMarkup:
    return _kb([[_o(texts.BTN_MERGE_GO.format(n=n), "merge_go", sid)],
                [_o(texts.BTN_MERGE_DEL, "merge_del", sid)],
                [_o(texts.BTN_CANCEL, "merge_cancel", sid)]])


def join_kb(sid: str) -> InlineKeyboardMarkup:
    return _kb([[_o(texts.BTN_JOIN_PLAIN, "merge_join", sid, "plain"),
                 _o(texts.BTN_JOIN_XFADE, "merge_join", sid, "xfade")],
                [_o(texts.BTN_CANCEL, "merge_cancel", sid)]])


def convert_formats(sid: str) -> InlineKeyboardMarkup:
    btns = [_o(f.upper(), "conv_fmt", sid, f) for f in ("mp3", "m4a", "ogg", "wav", "flac")]
    return _kb([*_chunks(btns, 3), _back(sid)])


def convert_bitrates(sid: str, fmt: str) -> InlineKeyboardMarkup:
    btns = [_o(f"{b}k", "conv_br", sid, f"{fmt}:{b}") for b in (128, 192, 256, 320)]
    return _kb([btns, _back(sid)])


def extract_kb(sid: str) -> InlineKeyboardMarkup:
    return _kb([[_o(texts.BTN_EXTRACT_ORIG, "ext_do", sid, "orig")],
                [_o(texts.BTN_EXTRACT_192, "ext_do", sid, "mp3_192"),
                 _o(texts.BTN_EXTRACT_320, "ext_do", sid, "mp3_320")], _back(sid)])


def tags_kb(sid: str) -> InlineKeyboardMarkup:
    return _kb([[_o(texts.TAG_TITLE, "tag", sid, "title"), _o(texts.TAG_ARTIST, "tag", sid, "artist"),
                 _o(texts.TAG_ALBUM, "tag", sid, "album")],
                [_o(texts.TAG_COVER, "tag", sid, "cover"), _o(texts.BTN_COVER_DEL, "tag", sid, "delcover")],
                [_o(texts.BTN_TAG_SHOW, "tag", sid, "show")], _back(sid)])


def ring_lengths(sid: str) -> InlineKeyboardMarkup:
    return _kb([[_o(f"{v} s", "ring_len", sid, str(v)) for v in (15, 20, 25, 30)], _back(sid)])


def ring_fades(sid: str) -> InlineKeyboardMarkup:
    return _kb([[_o(f"{v} s", "ring_fade", sid, str(v)) for v in (1, 2, 3)], _back(sid)])


def ring_targets(sid: str) -> InlineKeyboardMarkup:
    return _kb([[_o(texts.BTN_ANDROID, "ring_do", sid, "android"), _o(texts.BTN_IPHONE, "ring_do", sid, "iphone")],
                _back(sid)])


# ----------------------------------------------------------------------
# Instagram Reels Keyboards
# ----------------------------------------------------------------------

def reel_aspect_kb() -> InlineKeyboardMarkup:
    return _kb([
        [InlineKeyboardButton(text=texts.BTN_ASPECT_CROP, callback_data="ig_asp_crop")],
        [InlineKeyboardButton(text=texts.BTN_ASPECT_PAD, callback_data="ig_asp_pad")],
        [InlineKeyboardButton(text=texts.BTN_ASPECT_ORIG, callback_data="ig_asp_orig")],
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data="ig_cancel_flow")],
    ])


def reel_preview_kb(post_id: int) -> InlineKeyboardMarkup:
    return _kb([
        [
            InlineKeyboardButton(text=texts.BTN_CONFIRM, callback_data=f"ig_confirm_{post_id}"),
            InlineKeyboardButton(text=texts.BTN_REWRITE, callback_data=f"ig_rewrite_{post_id}"),
        ],
        [
            InlineKeyboardButton(text=texts.BTN_EDIT_CAPTION, callback_data=f"ig_edit_cap_{post_id}"),
            InlineKeyboardButton(text=texts.BTN_EDIT_HASHTAGS, callback_data=f"ig_edit_tags_{post_id}"),
        ],
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data=f"ig_cancel_{post_id}")],
    ])


def reel_time_kb(post_id: int) -> InlineKeyboardMarkup:
    return _kb([
        [InlineKeyboardButton(text=texts.BTN_TIME_AUTO, callback_data=f"ig_time_auto_{post_id}")],
        [
            InlineKeyboardButton(text=texts.BTN_TIME_NOW, callback_data=f"ig_time_now_{post_id}"),
            InlineKeyboardButton(text=texts.BTN_TIME_MANUAL, callback_data=f"ig_time_manual_{post_id}"),
        ],
        [InlineKeyboardButton(text=texts.BTN_CANCEL, callback_data=f"ig_cancel_{post_id}")],
    ])


def post_manage_kb(post_id: int) -> InlineKeyboardMarkup:
    return _kb([
        [
            InlineKeyboardButton(text=texts.BTN_RESCHEDULE, callback_data=f"ig_reschedule_{post_id}"),
            InlineKeyboardButton(text=texts.BTN_POST_NOW, callback_data=f"ig_post_now_{post_id}"),
        ],
        [
            InlineKeyboardButton(text=texts.BTN_EDIT_CAPTION, callback_data=f"ig_edit_cap_{post_id}"),
            InlineKeyboardButton(text=texts.BTN_DELETE_POST, callback_data=f"ig_cancel_{post_id}"),
        ],
    ])

