"""自动亮度曲线：按本地时间在「白天亮度 ↔ 夜间亮度」之间平滑过渡（纯函数，可单测）。

资源开销（用户问的核心，2026-11-10）：
- 取值复用走时 tick（显秒 1Hz / 关闭秒 1 次每分钟），一次曲线计算 = 十几次浮点运算，
  纳秒级，没有新增定时器、线程、磁盘 IO、网络；
- 只在「叠加 alpha 的整数值真变了」时才触发一次全窗重绘。两段过渡共 ~120 格亮度，
  最快处（黎明爬升）约 2 分钟才重绘一帧、每帧 alpha 最多挪 3/255 → 全天累计重绘
  一百多帧，相比本来就有的 1Hz 走时重绘可以忽略；
- 白天/深夜两段平台期 alpha 恒定，静止期依旧零重绘（与低配平板常驻的设计承诺一致）。

亮度上限就是屏幕本身的亮度：叠加黑色只能「压暗」不能「更亮」，
所以白天档位 = 100%（不压暗），真正的白天提亮要靠系统/硬件亮度。
"""
import os
from datetime import datetime

# 日程（本地时间，小时，可含小数）——过渡段两端各留 2~3 小时，避免日落时突然变暗
DAWN_START = 6.0    # 开始提亮
DAY_FULL = 8.0      # 完全白天
DUSK_START = 18.5   # 开始压暗
NIGHT_FULL = 21.5   # 完全夜间

BRIGHT_MIN, BRIGHT_MAX = 30, 100  # 与设置面板滑杆同区间

# AUTOTEST 专用伪小时：只在 import 时读一次环境变量（每次调用都读 os.environ 会白白
# 多花 3µs 量级——这函数是 1Hz 调的，没必要）
_FAKE_HOUR = None
if os.environ.get("FLIPCLOCK_AUTOTEST") == "1":
    try:
        _FAKE_HOUR = float(os.environ.get("FLIPCLOCK_FAKE_HOUR", "") or -1)
        if not 0.0 <= _FAKE_HOUR < 24.0:
            _FAKE_HOUR = None
    except ValueError:
        _FAKE_HOUR = None


def _now() -> datetime:
    """当前时间；AUTOTEST 下可用 FLIPCLOCK_FAKE_HOUR 伪造小时（验证白天/夜间两档）。"""
    base = datetime.now()
    if _FAKE_HOUR is None:
        return base
    return base.replace(hour=int(_FAKE_HOUR), minute=int((_FAKE_HOUR % 1.0) * 60))


def _smoothstep(t: float) -> float:
    """0→1 平滑过渡（两端导数为 0，肉眼看不到拐点）。"""
    t = min(1.0, max(0.0, t))
    return t * t * (3.0 - 2.0 * t)


def level_at(hour: float, day: float, night: float) -> float:
    """给定当地小时（0-24）返回应使用的亮度百分比（未取整）。

    曲线：夜间平台 → 黎明爬升 → 白天平台 → 黄昏下降 → 夜间平台。
    白天/夜间两个档位谁大谁小都成立（用户若把夜间调得比白天亮，曲线照样单调）。
    """
    hour = hour % 24.0
    if hour < DAWN_START or hour >= NIGHT_FULL:
        return night
    if hour <= DAY_FULL:                      # 黎明爬升：夜间 → 白天
        t = (hour - DAWN_START) / (DAY_FULL - DAWN_START)
        return night + (day - night) * _smoothstep(t)
    if hour <= DUSK_START:                    # 白天平台
        return day
    t = (hour - DUSK_START) / (NIGHT_FULL - DUSK_START)  # 黄昏下降：白天 → 夜间
    return day + (night - day) * _smoothstep(t)


def current_level(cfg: dict, now: datetime | None = None) -> int:
    """当前生效亮度（整数百分比）。自动开关关闭时就是手动档位，与时间无关。"""
    try:
        if not cfg.get("auto_bright", True):
            return int(_clamp(cfg.get("brightness", 100)))
        day = _clamp(cfg.get("bright_day", 100))
        night = _clamp(cfg.get("brightness", 100))
    except (TypeError, ValueError):
        return 100
    dt = now or _now()
    hour = dt.hour + dt.minute / 60.0
    return int(round(level_at(hour, day, night)))


def _clamp(v) -> float:
    return float(min(BRIGHT_MAX, max(BRIGHT_MIN, int(v))))


def overlay_alpha(level: int) -> int:
    """亮度百分比 → 全窗黑色叠加 alpha（未绘制的临界值返回 0）。"""
    return int(255 * (1.0 - level / 100.0))


IMPLS = ("backlight", "overlay")


def resolve_impl(cfg: dict, backlight_ok: bool) -> str:
    """决定亮度怎么落地：真背光（Power API）还是画面压暗。

    `backlight_ok` 由调用方（screen_bright.available()）传入——本模块保持零依赖纯函数，
    不 import ctypes，单测直接喂 True/False 即可覆盖两条路。
    auto：能调背光就用真背光（省电 + 夜里不刺眼），否则退回压暗。
    强制 backlight 但设备不支持时也退回压暗（有总比没有强）。
    """
    want = cfg.get("bright_impl", "auto")
    if want == "overlay":
        return "overlay"
    return "backlight" if backlight_ok else "overlay"
