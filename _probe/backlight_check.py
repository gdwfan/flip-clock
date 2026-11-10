# 真背光（Power API）验证：读得到、写得进、退得出。
#
# 这会**真的**改动本机屏幕亮度（100 → 62 → 原值），全程 try/finally 还原。
# 所以刻意不设 FLIPCLOCK_AUTOTEST（AUTOTEST 下 screen_bright 会拒绝写入）。
# 跑法：py -3.10 -X utf8 -I _probe/backlight_check.py
import os
import shutil
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"

_SRC = Path(__file__).resolve().parent.parent / "src"
shutil.rmtree(_SRC / "__pycache__", ignore_errors=True)
sys.path.insert(0, str(_SRC))

from PySide6.QtWidgets import QApplication  # noqa: E402

import brightness as bm  # noqa: E402
import screen_bright  # noqa: E402
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


def test_real_write():
    """真读真写真还原（本项会短暂改变屏幕亮度）。"""
    ok = screen_bright.available()
    check("Power API 可用（本机可调背光）", ok)
    if not ok:
        return
    original = screen_bright.get()
    print(f"   原始背光 = {original}%")
    try:
        check("写 100 生效", screen_bright.set(100) and screen_bright.get() == 100,
              f"读回 {screen_bright.get()}")
        check("写 62 生效", screen_bright.set(62) and screen_bright.get() == 62,
              f"读回 {screen_bright.get()}")
    finally:
        screen_bright.restore()
    check("还原到原值", screen_bright.get() == original,
          f"{screen_bright.get()} vs {original}")


def test_resolve_matrix():
    """落地方式选择：只有「能调背光且没被强制压暗」才用背光。"""
    check("auto + 支持 → 背光", bm.resolve_impl({}, True) == "backlight")
    check("auto + 不支持 → 压暗", bm.resolve_impl({}, False) == "overlay")
    check("强制背光 + 支持 → 背光", bm.resolve_impl({"bright_impl": "backlight"}, True) == "backlight")
    check("强制背光 + 不支持 → 压暗(兜底)", bm.resolve_impl({"bright_impl": "backlight"}, False) == "overlay")
    check("强制压暗 + 支持 → 压暗", bm.resolve_impl({"bright_impl": "overlay"}, True) == "overlay")
    check("脏值 → 当 auto", bm.resolve_impl({"bright_impl": "???"}, True) == "backlight")


def test_switch_real():
    """真机（本机）验证切换：从背光切到压暗，硬件亮度必须还给用户。

    不还原的后果是「背光已经降过 + 再叠一层黑」= 双重变暗，用户会觉得越切越暗。
    """
    if not screen_bright.available():
        print("   (本机不支持背光，跳过真机切换测试)")
        return
    app = QApplication.instance() or QApplication(sys.argv)
    original = screen_bright.get()
    try:
        cfg = dict(settings_mod.DEFAULTS)
        cfg.update({"auto_bright": False, "brightness": 55, "bright_impl": "backlight"})
        win = ClockWidget(cfg, lambda: {"status": "init", "offset_s": None})
        win.resize(1280, 800)
        win.show()
        app.processEvents()
        check("背光模式落 55%", screen_bright.get() == 55, f"{screen_bright.get()}%")

        cfg2 = dict(cfg, bright_impl="overlay")
        win.set_config(cfg2)
        app.processEvents()
        check("切压暗后硬件还原为原值", screen_bright.get() == original,
              f"{screen_bright.get()}% vs {original}%")
        check("切压暗后按叠加层计算", win._bright_impl == "overlay", win._bright_impl)

        cfg3 = dict(cfg2, bright_impl="backlight")
        win.set_config(cfg3)
        app.processEvents()
        check("切回背光重新写入", screen_bright.get() == 55, f"{screen_bright.get()}%")
        win.close()
        screen_bright.restore()
        check("收尾还原", screen_bright.get() == original, f"{screen_bright.get()}%")
    finally:
        screen_bright.restore()


def test_widget_wiring():
    """接线：背光模式写背光且不重绘；压暗模式才重绘；切换时释放硬件亮度。"""
    app = QApplication.instance() or QApplication(sys.argv)
    calls, restores = [], []
    orig_set, orig_avail, orig_restore = (screen_bright.set, screen_bright.available,
                                          screen_bright.restore)

    def fake_set(level):
        calls.append(level)
        return True

    screen_bright.available = lambda: True
    screen_bright.set = fake_set
    screen_bright.restore = lambda: restores.append(1) or True
    try:
        cfg = dict(settings_mod.DEFAULTS)
        cfg.update({"auto_bright": False, "brightness": 70})
        win = ClockWidget(cfg, lambda: {"status": "init", "offset_s": None})
        win.resize(1280, 800)
        updates = []
        orig_update = win.update
        win.update = lambda *a: updates.append(a) or orig_update(*a)
        win.show()
        app.processEvents()
        check("构造即解析为背光", win._bright_impl == "backlight", win._bright_impl)
        check("show 时写了一次背光", calls == [70], str(calls))
        # 只断言「亮度这条路径不重绘」：show 期间走时/布局本来就会 update 若干次，
        # 拿总次数当判据等于测别的东西
        updates.clear()
        win._refresh_brightness(force=True)
        check("背光模式刷亮度零重绘", updates == [], str(len(updates)))
        # 拖滑杆：合并写入（150ms 防抖），不逐次打 ACPI
        calls.clear()
        win._cfg["brightness"] = 55
        win._refresh_brightness()
        check("拖动期间不立刻写", calls == [], str(calls))
        win._apply_backlight()
        check("防抖后写最终值", calls == [55], str(calls))
        # 切到压暗：必须先 release 硬件亮度
        restores.clear()
        calls.clear()
        win.set_config(dict(win._cfg, bright_impl="overlay"))
        check("切压暗释放硬件亮度", restores == [1], str(restores))
        check("切压暗后不再写背光", calls == [], str(calls))
        # 切回背光：重新写当前档位
        calls.clear()
        win.set_config(dict(win._cfg, bright_impl="backlight"))
        check("切回背光立刻写当前档位", calls == [55], str(calls))
        # 压暗模式无论如何都要重绘
        win._bright_impl = "overlay"
        updates.clear()
        win._cfg["brightness"] = 40
        win._refresh_brightness(force=True)
        check("压暗模式触发全窗重绘", len(updates) == 1, str(len(updates)))
        win.close()
    finally:
        screen_bright.set, screen_bright.available, screen_bright.restore = (
            orig_set, orig_avail, orig_restore)


def main():
    print("== 1. 真背光读写（会短暂改变屏幕亮度，结束即还原）")
    test_real_write()
    print("== 2. 落地方式选择矩阵")
    test_resolve_matrix()
    print("== 3. 真机切换背光↔压暗（会短暂改变屏幕亮度）")
    test_switch_real()
    print("== 4. 控件接线（不碰真背光）")
    test_widget_wiring()
    print(f"BACKLIGHT_CHECK pass={PASS} fail={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
