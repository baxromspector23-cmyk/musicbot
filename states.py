"""FSM state groups."""
from aiogram.fsm.state import State, StatesGroup


class CutStates(StatesGroup):
    waiting_range = State()


class FadeStates(StatesGroup):
    waiting_seconds = State()


class SpeedStates(StatesGroup):
    waiting_value = State()


class PitchStates(StatesGroup):
    waiting_value = State()


class VolumeStates(StatesGroup):
    waiting_value = State()


class MergeStates(StatesGroup):
    collecting = State()
    choosing_join = State()


class TagStates(StatesGroup):
    choosing_field = State()
    waiting_text = State()
    waiting_cover = State()


class RingStates(StatesGroup):
    waiting_start = State()
    choosing = State()


class IgStates(StatesGroup):
    waiting_video = State()
    waiting_aspect = State()
    waiting_description = State()
    editing_caption = State()
    editing_hashtags = State()
    waiting_time = State()
    editing_time = State()

