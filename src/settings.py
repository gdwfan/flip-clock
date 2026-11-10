"""配置读写：%APPDATA%\\FlipClock\\config.json，原子写 + .bak 轮换 + 损坏自愈。

原子写三件套：写走 tmp→rename；损坏文件隔离成 .corrupt-*；从 .bak 恢复后立即写回正式文件。
AUTOTEST 模式下重定向到临时目录，避免测试写脏用户真实配置。
"""
import json
import os
import tempfile
import time
from pathlib import Path

APP_NAME = "FlipClock"

if os.environ.get("FLIPCLOCK_AUTOTEST") == "1":
    CONF_DIR = Path(tempfile.mkdtemp(prefix="flipclock-test-cfg-"))
else:
    CONF_DIR = Path(os.environ.get("APPDATA") or Path.home()) / APP_NAME

CONF_FILE = CONF_DIR / "config.json"
_BAK = CONF_FILE.with_name(CONF_FILE.name + ".bak")
_TMP = CONF_FILE.with_name(CONF_FILE.name + ".tmp")

DEFAULTS = {
    "fmt24": True,          # True=24小时制
    "seconds_mode": "big",  # 秒显示：big 同大 / small 小一号 / off 关闭
    "show_seconds": True,   # 旧字段，仅为兼容历史配置保留
    "show_date": True,
    "flip_anim": True,      # 翻页动画（可在设置关闭省电）
    "auto_bright": True,    # 自动亮度：白天亮、夜间暗，按时间平滑过渡
    "brightness": 100,      # 夜间亮度（自动模式）/ 屏幕亮度（手动模式）30-100
    "bright_day": 100,      # 白天亮度 30-100
    "bright_impl": "auto",  # 亮度落地方式：auto 优先真背光 / backlight 强制 / overlay 画面压暗
    "keep_awake": True,     # 屏幕常亮
    "ntp_check": True,      # 每日 NTP 偏差校验（2026-11-08 拍板保留）
    "autostart": False,     # 开机自启（HKCU Run）
    "first_run_done": False,
}


def load():
    """读取配置；损坏时隔离现场并从 .bak 恢复（恢复值立刻写回正式文件）。"""
    data = dict(DEFAULTS)
    raw = None
    try:
        raw = CONF_FILE.read_text("utf-8")
        data.update(json.loads(raw))
    except FileNotFoundError:
        pass
    except Exception:
        # 损坏：隔离现场，别直接当新配置
        try:
            stamp = int(time.time())
            CONF_FILE.rename(CONF_FILE.with_name(f"{CONF_FILE.name}.corrupt-{stamp}"))
        except OSError:
            pass
        try:
            data.update(json.loads(_BAK.read_text("utf-8")))
        except Exception:
            data = dict(DEFAULTS)
        save(data)  # 恢复值（或全新默认）立刻写回正式文件——只返回不落盘等于没恢复
    # 数值归一
    try:
        data["brightness"] = min(100, max(30, int(data.get("brightness", 100))))
    except (TypeError, ValueError):
        data["brightness"] = 100
    try:
        data["bright_day"] = min(100, max(30, int(data.get("bright_day", 100))))
    except (TypeError, ValueError):
        data["bright_day"] = 100
    data["auto_bright"] = bool(data.get("auto_bright", True))
    if data.get("bright_impl") not in ("auto", "backlight", "overlay"):
        data["bright_impl"] = "auto"
    # 秒显示档位归一（兼容只有旧布尔字段的历史配置）
    if data.get("seconds_mode") not in ("big", "small", "off"):
        data["seconds_mode"] = "big" if data.get("show_seconds", True) else "off"
    return data


def save(data):
    """原子写：tmp → 轮换 .bak → rename 落盘。"""
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    _TMP.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    if CONF_FILE.exists():
        try:
            os.replace(CONF_FILE, _BAK)
        except OSError:
            pass
    os.replace(_TMP, CONF_FILE)
