# 复查回归：覆盖 2026-11-08 系统性复查发现的缺陷，每条都有可执行断言。
#
# 跑法：py -3.10 -X utf8 _probe/defects_check.py   （不开窗口，不打扰桌面）
import os
import shutil
import sys
import threading
import time
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "windows"
os.environ["FLIPCLOCK_AUTOTEST"] = "1"

_ROOT = Path(__file__).resolve().parent.parent
_SRC = _ROOT / "src"
shutil.rmtree(_SRC / "__pycache__", ignore_errors=True)  # 防 stale pyc
sys.path.insert(0, str(_SRC))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import settings as settings_mod  # noqa: E402
from clock_widget import ClockWidget  # noqa: E402
from debounce import SaveDebouncer  # noqa: E402
from ntp_check import NtpChecker  # noqa: E402

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


def pump(seconds):
    """推进事件循环。先 sleep 再 processEvents——顺序反了会让刚启动的
    QTimer 在到期前就被跳过（对照组曾经因此写出 0 次，是测试自身的坑）。"""
    t0 = time.time()
    while time.time() - t0 < seconds:
        time.sleep(0.02)
        app.processEvents()


app = QApplication(sys.argv)
cfg = settings_mod.load()

# ---------- 1) Esc 退出全屏通道：widget 必须能接收键盘事件 ----------
win = ClockWidget(cfg, lambda: {"status": "init", "offset_s": None})
check("focus policy accepts keyboard",
      win.focusPolicy() != Qt.FocusPolicy.NoFocus,
      f"policy={win.focusPolicy()}")

# ---------- 2) 关闭网络校时后，徽标必须回到「本地时间」----------
cfg_on = dict(cfg)
cfg_on["ntp_check"] = True
win.set_config(cfg_on)
synced_snap = lambda: {"status": "ok", "offset_s": 0.2, "last_check": None, "server": "x"}  # noqa: E731
win2 = ClockWidget(cfg_on, synced_snap)
check("badge shows sync when enabled",
      win2._badge_state()[1].startswith("NTP 已同步"), win2._badge_state()[1])
cfg_off = dict(cfg)
cfg_off["ntp_check"] = False
win2.set_config(cfg_off)
check("badge reverts to local when disabled",
      win2._badge_state()[1] == "本地时间", win2._badge_state()[1])

# ---------- 3) 徽标内容未变时不得重绘（保持静止期零重绘）----------
win3 = ClockWidget(cfg_on, synced_snap)
win3.resize(1000, 720)
win3._relayout()
calls = []
orig_update = win3.update


def spy(*a):
    calls.append(a)
    return orig_update(*a)


win3.update = spy
for _ in range(5):
    win3._poll_ntp()
win3.update = orig_update
# 首次允许重绘一次（缓存为空），此后内容不变应零重绘
check("badge repaints only on change", len(calls) <= 1, f"update_calls={len(calls)}")

# ---------- 4) 落盘防抖：连续 25 次变更只写一次 ----------
saved = []
snap_box = {"b": 0}
d = SaveDebouncer(lambda: snap_box, interval_ms=150, saver=lambda c: saved.append(c))
for i in range(25):
    snap_box["b"] = i
    d.touch()
    app.processEvents()
check("debounce suppresses burst writes", len(saved) == 0, f"saved={len(saved)}")
pump(0.5)
check("debounce writes once after quiet", len(saved) == 1, f"saved={len(saved)}")
check("debounce wrote latest value", saved and saved[0]["b"] == 24, f"{saved}")
# 对照组：几乎无防抖时应产生大量写（证明断言真的在测防抖行为）
saved2 = []
d2 = SaveDebouncer(lambda: snap_box, interval_ms=1, saver=lambda c: saved2.append(c))
for i in range(25):
    snap_box["b"] = i
    d2.touch()
    pump(0.01)
check("control group without debounce writes repeatedly", len(saved2) > 5, f"saved={len(saved2)}")

# ---------- 5) NTP 线程代际：反复开关不得留下多个线程 ----------
c = NtpChecker()
for _ in range(3):
    c.start(delay_s=30)   # delay 长，不会真发网络请求
    time.sleep(0.05)
    c.stop()
    time.sleep(0.15)
alive = [t for t in threading.enumerate() if t.name.startswith("flipclock-ntp")]
check("no thread leak on restart", len(alive) <= 1, f"alive={[t.name for t in alive]}")
c.stop()
pump(0.3)
alive_after = [t for t in threading.enumerate() if t.name.startswith("flipclock-ntp")]
check("threads retire after stop", len(alive_after) == 0, f"{[t.name for t in alive_after]}")

# ---------- 6) 资源路径：开发期命中，且与打包 add-data 目标一致 ----------
import main as main_mod  # noqa: E402

ico = main_mod._asset_path("icon.ico")
check("icon path resolves in dev", os.path.exists(ico), ico)
build_src = (_ROOT / "build_app.py").read_text("utf-8")
check("build add-data target matches code path",
      ";assets" in build_src,
      "add-data 的落点必须是 assets（与 _asset_path 的 assets/ 及 sys._MEIPASS 基址一致）")

# ---------- 7) 退出全屏后窗口几何合理（屏宽 70% 居中）----------
win4 = ClockWidget(dict(cfg), lambda: {"status": "init", "offset_s": None})
win4.resize(400, 300)
win4.show()
app.processEvents()
win4.prepare_windowed()
area = win4.screen().availableGeometry()
expect_w, expect_h = int(area.width() * 0.7), int(area.height() * 0.7)
check("windowed geometry approx 70% of screen",
      abs(win4.width() - expect_w) < 40 and abs(win4.height() - expect_h) < 40,
      f"got={win4.width()}x{win4.height()} expect={expect_w}x{expect_h}")
cx_ok = abs(win4.frameGeometry().center().x() - area.center().x()) < 60
check("windowed geometry centered", cx_ok,
      f"win_cx={win4.frameGeometry().center().x()} screen_cx={area.center().x()}")
# 只生效一次：再次调用不应改动用户手动调整过的尺寸
win4.resize(500, 400)
win4.prepare_windowed()
check("windowed geometry applied only once", win4.width() == 500, f"{win4.width()}")
win4.hide()

print(f"DEFECTS_TESTS pass={PASS} fail={FAIL}")
sys.exit(1 if FAIL else 0)
