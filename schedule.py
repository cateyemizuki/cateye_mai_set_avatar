"""每日随机头像插件 — 触发时间与漂移解析（纯逻辑，不依赖 SDK，可离线测试）。"""

from __future__ import annotations

import random
import re
from datetime import datetime, time as dt_time, timedelta

DEFAULT_TRIGGER_TIME = "08:30"
DEFAULT_DRIFT_SPEC = '"-45"-"30"'

# HH:MM（允许 H:MM）。
_TRIGGER_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
# 漂移范围格式固定为 "a"-"b"：两侧必须带英文双引号、中间必须是单个 -，
# 缺引号或缺中间的 - 都视为格式错误（不收）。
_DRIFT_RE = re.compile(r'^"(-?\d+)"-"(-?\d+)"$')


def parse_trigger_time(value: object) -> dt_time | None:
    """解析 HH:MM 触发时间；非法时返回 None。"""
    raw = str(value or "").strip()
    match = _TRIGGER_RE.match(raw)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return dt_time(hour, minute)


def parse_drift_spec(value: object) -> tuple[int, int] | None:
    """解析漂移范围 "-a"-"b"（分钟）；格式错误时返回 None。

    负数代表向前（提前）漂移。返回 (low, high)，low <= high。
    """
    raw = str(value or "").strip()
    match = _DRIFT_RE.match(raw)
    if not match:
        return None
    low, high = int(match.group(1)), int(match.group(2))
    return (low, high) if low <= high else (high, low)


def roll_planned_datetime(
    trigger_time: dt_time,
    drift_range: tuple[int, int],
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> tuple[datetime, int]:
    """在触发时间上叠加随机漂移，得到今天的计划时间点。

    Returns:
        (计划时间点, 实际抽取的漂移分钟数)。
    """
    now = now or datetime.now()
    chooser = rng or random
    low, high = drift_range
    minutes = chooser.randint(low, high)
    planned = datetime.combine(now.date(), trigger_time) + timedelta(minutes=minutes)
    return planned, minutes


def format_drift_minutes(minutes: int) -> str:
    """把漂移分钟数格式化为可读文本。"""
    if minutes == 0:
        return "无漂移"
    if minutes > 0:
        return f"+{minutes} 分钟"
    return f"{minutes} 分钟（提前）"
