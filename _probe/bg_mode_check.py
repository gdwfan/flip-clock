# 后台模式（--bg）验证：开发机上「一边干活一边看效果」时，窗口不许抢焦点、不许盖住当前窗口。
#
# 只验 bg 路径。**不验**全屏路径——那会真的在你桌面上弹一个全屏窗口把焦点抢走，
# 而这个探针存在的意义恰恰是"别打扰用户"（真机全屏在平板上验）。
# 会短暂出现一个窗口化时钟，不抢焦点；跑完即关。
#
# 跑法：py -3.10 -X utf8 -I _probe/bg_mode_check.py
import ctypes
import os
import shutil
import sys
from pathlib import Path

os.environ["FLIPCLOCK_AUTOTEST"] = "1"  # 配置写临时目录、不碰常亮/背光

_SRC = Path(__file__).resolve().parent.parent / "src"
shutil.rmtree(_SRC / "__pycache__", ignore_errors=True)
sys.path.insert(0, str(_SRC))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import main as main_mod  # noqa: E402
import settings as settings_mod  # noqa: E402
from clock_widget import ClockWidget  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {detail}")


def z_position(hwnd):
    """本窗口在可见顶层窗口 z 序里的位次（0 = 最上层）；仅作诊断打印，不做判据。"""
    u = ctypes.windll.user32
    h, i = u.GetTopWindow(None), 0
    while h:
        if u.IsWindowVisible(h):
            if h == hwnd:
                return i
            i += 1
        h = u.GetWindow(h, 2)  # GW_HWNDNEXT
    return -1


def main():
    app = QApplication(sys.argv)
    cfg = settings_mod.load()
    win = ClockWidget(cfg, lambda: {"status": "init", "offset_s": None})
    win.setWindowTitle("翻页时钟 · 后台模式自检")

    fg_before = ctypes.windll.user32.GetForegroundWindow()
    main_mod._start_window(win, True)
    app.processEvents()

    hwnd = int(win.winId())
    fg_after = ctypes.windll.user32.GetForegroundWindow()

    check("不是全屏", not win.isFullScreen())
    check("设了 WA_ShowWithoutActivating",
          win.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating))
    check("窗口没有被激活", not win.isActiveWindow())
    check("前台窗口没被抢走", fg_after == fg_before,
          f"fg {fg_before} -> {fg_after}（本窗口 {hwnd}）")
    check("前台窗口不是本窗口", fg_after != hwnd)

    scr = win.screen().availableGeometry()
    check("窗口化尺寸 ≈ 屏的 70%",
          abs(win.width() - int(scr.width() * 0.7)) <= 2
          and abs(win.height() - int(scr.height() * 0.7)) <= 2,
          f"{win.width()}x{win.height()} vs {scr.width()}x{scr.height()}")
    # 用 frameGeometry（geometry() 是客户区、不含标题栏），容差给 ±20px：
    # move() 定位带框整窗、resize() 定的是客户区高，两者差一个标题栏高度的一半（实测 15px），
    # 属既有行为的视觉误差，不在本次"别抢焦点"的改动范围里
    check("窗口居中（±20px）",
          abs(win.frameGeometry().center().x() - scr.center().x()) <= 20
          and abs(win.frameGeometry().center().y() - scr.center().y()) <= 20,
          f"{win.frameGeometry().center()} vs {scr.center()}")

    print(f"  (诊断) 本窗口 z 序位次 = {z_position(hwnd)}（>0 表示没压在最上层；不做判据，"
          f"桌面窗口结构随用户环境变化）")
    print(f"  (诊断) 前台窗口 = {fg_after}")
    win.close()
    return 1 if FAIL else 0


if __name__ == "__main__":
    code = main()
    print(f"BG_MODE_CHECK pass={PASS} fail={FAIL}")
    sys.exit(code)
