"""时间调度：格式化 + 秒对齐计算。显示时间 100% 来自系统本地时钟（方案 §1.2 决策 B）。"""
from datetime import datetime

WEEK_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]


def digits_for(dt: datetime, fmt24: bool):
    """返回 (HHMMSS 六字符, 'AM'/'PM')。12 小时制时 HH 已换算。"""
    h = dt.hour
    ap = "AM" if h < 12 else "PM"
    if not fmt24:
        h = h % 12
        if h == 0:
            h = 12
    return f"{h:02d}{dt.minute:02d}{dt.second:02d}", ap


def format_date(dt: datetime) -> str:
    return f"{dt.year}年{dt.month}月{dt.day}日 · {WEEK_CN[dt.isoweekday() - 1]}"


def ms_to_next_tick(now: datetime, with_seconds: bool) -> int:
    """距下一次需要的跳变还有多少毫秒（对齐墙钟，不累积漂移）。"""
    if with_seconds:
        return 1000 - now.microsecond // 1000
    us = (59 - now.second) * 1_000_000 + (1_000_000 - now.microsecond)
    return us // 1000
