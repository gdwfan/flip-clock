# 自动亮度的资源代价（回答「这个功能费资源吗」）——分解成单位成本 × 发生次数。
#
# 为什么不直接 A/B 比总耗时：同一场景连测三次得 0.11 / 0.53 / 0.81 秒 CPU，
# 相差 7 倍；连离屏跑的 paintEvent 次数都是 17 / 40 / 17——量到的是桌面合成、
# 窗口遮挡、系统负载的噪声，信号全埋在噪声里（2026-11-10 实测，两版脚本都作废）。
# 换成不依赖「另一件东西做对照」的口径：
#   A 算一次要多少：current_level() 单次耗时（µs）
#   B 一天发生多少次：按分钟模拟 24 小时，数亮度整数值变化次数（=额外全窗重绘次数）
#   C 重绘一帧要多少：同步 repaint 一帧的中位耗时（ms）
#   D 接线对不对：亮度值没变时必须不发全窗重绘（白盒，不靠计时）
# 结论口径：额外 CPU/天 = B × C + 86400 × A。
#
# 跑法：py -3.10 -X utf8 -I _probe/bright_cost.py
import os
import shutil
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"  # 不碰桌面合成
os.environ["FLIPCLOCK_AUTOTEST"] = "1"

_SRC = Path(__file__).resolve().parent.parent / "src"
shutil.rmtree(_SRC / "__pycache__", ignore_errors=True)
sys.path.insert(0, str(_SRC))

from PySide6.QtWidgets import QApplication  # noqa: E402

import brightness as bm  # noqa: E402
import settings as settings_mod  # noqa: E402
from clock_widget import ClockWidget  # noqa: E402

PASS = 0
FAIL = 0
AUTO_CFG = {"auto_bright": True, "bright_day": 100, "brightness": 40}


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def bench_pure():
    """A：单次取值耗时。"""
    n = 20000
    t0 = time.perf_counter()
    for _ in range(n):
        bm.current_level(AUTO_CFG)
    dt = (time.perf_counter() - t0) / n * 1e6
    print(f"  A 单次 current_level = {dt:.2f} µs → 1Hz 走时一天 = {dt * 86400 / 1e6:.2f}s CPU")
    check("A 单次取值 < 50µs", dt < 50, f"{dt:.2f}µs")
    return dt


def count_day_changes(cfg):
    """B：全天 24 小时里亮度整数值变化几次（每次变化 = 一次额外全窗重绘）。"""
    base = datetime(2026, 1, 1)
    vals = [bm.current_level(cfg, base + timedelta(minutes=m)) for m in range(24 * 60)]
    return sum(1 for a, b in zip(vals, vals[1:]) if a != b), vals


def bench_day():
    changes, vals = count_day_changes(AUTO_CFG)
    print(f"  B 全天变化 {changes} 次（额外全窗重绘）· 亮度区间 {min(vals)}-{max(vals)}%")
    check("B 全天额外重绘 <= 200 次", changes <= 200, f"{changes}")
    # 平台期（白天/深夜）必须一次都不变：一天里只有两段过渡会重绘
    changing = [m for m in range(24 * 60) if vals[min(m, 1438)] != vals[min(m + 1, 1439)]]
    hours = sorted({m // 60 for m in changing})
    check("B 只在过渡段变化", set(hours) <= {6, 7, 18, 19, 20, 21}, f"hours={hours}")
    return changes


def bench_repaint(app):
    """C：同步重绘一帧全窗的中位耗时。"""
    cfg = dict(settings_mod.DEFAULTS)
    cfg.update({"seconds_mode": "big", "auto_bright": True, "brightness": 40,
                "bright_day": 100})
    win = ClockWidget(cfg, lambda: {"status": "init", "offset_s": None})
    win.resize(1280, 800)
    win.show()
    app.processEvents()
    for _ in range(5):  # 预热：字体/字形位图缓存建好再计时
        win.repaint()
    samples = []
    for _ in range(30):
        t0 = time.perf_counter()
        win.repaint()
        samples.append((time.perf_counter() - t0) * 1000.0)
    ms = statistics.median(samples)
    print(f"  C 全窗重绘一帧 = {ms:.1f}ms（中位，n=30，1280x800 离屏）")
    win.close()
    return ms


def check_wiring(app):
    """D：亮度值不变 → 不发全窗重绘；变了 → 恰好一次。"""
    cfg = dict(settings_mod.DEFAULTS)
    cfg.update({"auto_bright": True, "brightness": 40, "bright_day": 100})
    win = ClockWidget(cfg, lambda: {"status": "init", "offset_s": None})
    win.resize(1280, 800)
    win.show()
    app.processEvents()
    win._bright_now = 50  # 起点钉死，否则真实时间算出的初值会多记一次变化

    seq = [50, 50, 50, 51, 51, 52, 52]
    idx = [0]

    def fake(cfg_, now=None):
        v = seq[min(idx[0], len(seq) - 1)]
        idx[0] += 1
        return v

    fulls = []
    orig_update = win.update

    def spy(*args):
        if not args:
            fulls.append(1)
        return orig_update(*args)

    orig_level = bm.current_level
    bm.current_level = fake
    win.update = spy
    try:
        for _ in range(len(seq)):
            win._refresh_brightness()
    finally:
        bm.current_level = orig_level
        win.update = orig_update
        win.close()
    # 50→51、51→52 各一次；同值调用一次都不许重绘
    check("D 值变才重绘且只重绘一次", len(fulls) == 2, f"fulls={len(fulls)}")
    check("D 当前值跟随", win._bright_now == 52, str(win._bright_now))


def main():
    app = QApplication(sys.argv)
    us = bench_pure()
    changes = bench_day()
    ms = bench_repaint(app)
    check_wiring(app)

    per_day_s = changes * ms / 1000.0 + 86400 * us / 1e6
    print(f"  合计：额外 CPU ≈ {per_day_s:.1f}s/天 = 单核的 "
          f"{per_day_s / 86400 * 100:.4f}%（其中重绘占 {changes * ms / 1000.0:.1f}s）")
    check("额外 CPU <= 10s/天", per_day_s <= 10.0, f"{per_day_s:.1f}s")
    print(f"BRIGHT_COST pass={PASS} fail={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
