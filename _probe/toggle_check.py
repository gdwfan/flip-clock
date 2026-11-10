# 设置切换回归：验证「显隐秒 / 显隐日期 / 24-12 切换」后，界面会立刻整体重绘，
# 而不是等最小化、恢复这类强制 expose 才自愈（2026-11-08 用户真机踩到）。
#
# 为什么是白盒（检查 update 脏区覆盖）而不是抓屏幕像素：
#   1. 抓屏要求窗口可见且在最前——用户正常用电脑时测试窗口会被遮挡，
#      断言会采到别的画面（2026-11-08 实测：抓到了浏览器窗口），产生假绿/假红；
#   2. win.grab() 会强制整窗重绘，恰好掩盖「局部脏区漏覆盖」这个缺陷本身。
#   白盒直击根因：布局/显隐一变，【切换前】的每个绘制矩形都必须落在
#   【切换后】的某个重绘区域里，否则旧像素会残留在屏幕上不动。
#
# 跑法：py -3.10 -X utf8 _probe/toggle_check.py   （不开窗口，不打扰当前桌面）
import os
import shutil
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "windows"
os.environ["FLIPCLOCK_AUTOTEST"] = "1"

_SRC = Path(__file__).resolve().parent.parent / "src"
shutil.rmtree(_SRC / "__pycache__", ignore_errors=True)  # 防 stale pyc
sys.path.insert(0, str(_SRC))

from PySide6.QtCore import QRect, QRectF  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import settings as settings_mod  # noqa: E402
import screen_bright  # noqa: E402
from clock_widget import ClockWidget  # noqa: E402
from settings_panel import SettingsPanel  # noqa: E402

# 本机能力由硬件决定，测试里必须自己钉死：AUTOTEST 下 available() 恒为 False，
# 「背光」按钮会被禁用、点不动，切换用例就白测了。这里伪造"能调背光"，
# 且 set/restore 都换成假实现 —— 白盒测重绘覆盖，不该真去动屏幕亮度。
screen_bright.available = lambda: True
screen_bright.set = lambda level: True
screen_bright.restore = lambda: True

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


app = QApplication(sys.argv)
cfg = settings_mod.load()
snap = lambda: {"status": "init", "offset_s": None}  # noqa: E731
win = ClockWidget(cfg, snap)
panel = SettingsPanel(cfg, snap, parent=win)
# 复刻 main.py 接线：切换必须走真实控件 → config_changed → set_config 链路
panel.config_changed.connect(lambda c: win.set_config(c))
win.resize(1000, 720)
app.processEvents()


def regions_now():
    """当前布局下会被绘制的全部矩形。"""
    regs = list(win._rects.values()) + list(win._colon_rects)
    if win._cfg.get("show_date", True):
        regs.append(win._date_rect)
    return list(regs)


def capture_updates(trigger):
    """执行 trigger，返回期间所有 update() 调用的参数列表。"""
    calls = []
    orig = win.update

    def spy(*args):
        calls.append(args)
        return orig(*args)

    win.update = spy
    try:
        trigger()
    finally:
        win.update = orig
    return calls


def covered(rect, calls):
    """rect 是否被某个重绘区域覆盖（无参 update() = 全窗，覆盖一切）。"""
    for a in calls:
        if len(a) == 0:
            return True
        if len(a) == 1:
            arg = a[0]
            if isinstance(arg, QRect) and QRectF(arg).contains(rect):
                return True
            if isinstance(arg, QRectF) and arg.contains(rect):
                return True
        if len(a) == 4:
            if QRectF(a[0], a[1], a[2], a[3]).contains(rect):
                return True
    return False


def case(name, trigger):
    """切换前收集全部绘制矩形，切换后断言它们都被重绘覆盖。"""
    old = regions_now()
    calls = capture_updates(trigger)
    uncovered = [r for r in old if not covered(r, calls)]
    full = any(len(a) == 0 for a in calls)
    check(name, not uncovered,
          f"update_calls={len(calls)} full_window={full} uncovered={len(uncovered)}"
          + (f" first={uncovered[0].toRect()}" if uncovered else ""))


# ---- 场景（覆盖用户报告的全部三种切换）----
case("seconds off repaints all old layout",
     lambda: panel._sec_btns["off"].click())
case("seconds on repaints all old layout",
     lambda: panel._sec_btns["big"].click())
case("date off repaints all old layout",
     lambda: panel._sw_date.setChecked(False, emit=True))
case("date on repaints all old layout",
     lambda: panel._sw_date.setChecked(True, emit=True))
case("12h switch repaints all old layout",
     lambda: panel._b12.click())
case("24h switch repaints all old layout",
     lambda: panel._b24.click())
case("brightness change repaints",
     lambda: panel._slider.setValue(60))
case("day brightness change repaints",
     lambda: panel._day_slider.setValue(55))
case("auto brightness off repaints (row layout changes)",
     lambda: panel._sw_auto.setChecked(False, emit=True))
case("auto brightness on repaints (row layout changes)",
     lambda: panel._sw_auto.setChecked(True, emit=True))
case("impl -> overlay repaints",
     lambda: panel._impl_btns["overlay"].click())
case("impl -> backlight repaints",
     lambda: panel._impl_btns["backlight"].click())

print(f"TOGGLE_TESTS pass={PASS} fail={FAIL}")
sys.exit(1 if FAIL else 0)
