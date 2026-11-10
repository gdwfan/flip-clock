# 打包态冒烟：直接跑 dist 里的 exe，验证「瘦身没删错、启动正常、进程树干净」。
#
# 跑法：py -3.10 -X utf8 _probe/packaged_smoke.py
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
EXE = ROOT / "dist" / "FlipClock" / "FlipClock.exe"
SHOTS = ROOT / "_probe" / "shots"
SHOTS.mkdir(parents=True, exist_ok=True)

PASS = 0
FAIL = 0


def win_work_area():
    """屏幕工作区（物理像素，已扣任务栏）。无第三方依赖，直接问 Windows。"""
    import ctypes
    from ctypes import wintypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                    ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    r = RECT()
    ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA
    return r.right - r.left, r.bottom - r.top


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def exe_processes():
    """当前存活的 FlipClock.exe 进程（按镜像名，不靠命令行）。"""
    out = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if (p.info["name"] or "").lower() == "flipclock.exe":
                out.append(p.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return out


def kill_tree():
    """整树终止：terminate → wait → kill，再按镜像名兜底清扫。"""
    victims = [psutil.Process(pid) for pid in exe_processes()]
    for p in victims:
        try:
            for child in p.children(recursive=True):
                try:
                    child.terminate()
                except psutil.NoSuchProcess:
                    pass
            p.terminate()
        except psutil.NoSuchProcess:
            pass
    gone, alive = psutil.wait_procs(victims, timeout=5)
    for p in alive:
        try:
            p.kill()
        except psutil.NoSuchProcess:
            pass
    time.sleep(1.0)
    for pid in exe_processes():  # 按镜像名兜底
        try:
            psutil.Process(pid).kill()
        except psutil.NoSuchProcess:
            pass


def main():
    check("exe exists", EXE.is_file(), str(EXE))
    if not EXE.is_file():
        print("PKG_SMOKE aborted")
        return 1

    # exe 体积与关键资源
    total = sum(f.stat().st_size for f in (ROOT / "dist" / "FlipClock").rglob("*") if f.is_file())
    print(f"  info package size = {total / 1048576:.1f} MB")
    internal = ROOT / "dist" / "FlipClock" / "_internal"
    check("slim kept qwindows platform plugin",
          (internal / "PySide6/plugins/platforms/qwindows.dll").is_file())
    check("slim kept ico image plugin",
          (internal / "PySide6/plugins/imageformats/qico.dll").is_file())
    check("slim removed opengl32sw",
          not (internal / "PySide6/opengl32sw.dll").exists())
    check("slim removed translations",
          not (internal / "PySide6/translations").exists())

    # ---- 场景 A：AUTOTEST 启动 → 截图 → 退出码 0 ----
    png = SHOTS / "packaged_default.png"
    if png.exists():
        png.unlink()
    env = dict(os.environ)
    env["FLIPCLOCK_AUTOTEST"] = "1"
    env["FLIPCLOCK_SCREENSHOT"] = str(png)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    r = subprocess.run([str(EXE)], env=env, timeout=120,
                       capture_output=True, encoding="utf-8", errors="replace")
    check("packaged exe exits 0 (rendered)", r.returncode == 0, f"exit={r.returncode}")
    check("packaged screenshot written", png.is_file(),
          f"{png.stat().st_size if png.exists() else 0} bytes")

    if png.is_file():
        from PIL import Image
        im = Image.open(png).convert("L")
        w, h = im.size
        # 基准是 1280x800，但**不能假设屏幕装得下**：桌面更小时 AUTOTEST 窗口会被钳住
        # （GitHub runner 桌面 1024x768 → 实测窗口 1028x749）。按屏幕工作区取 min，
        # 窄屏机器不再假失败。（CI 首次运行就抓到这一类假设）
        work_w, work_h = win_work_area()
        exp_w, exp_h = min(1600, work_w), min(1000, work_h)
        check("screenshot size sane",
              w >= exp_w * 0.98 and h >= exp_h * 0.98,
              f"{w}x{h}（期望 ≥ {exp_w}x{exp_h}；屏幕工作区 {work_w}x{work_h}）")
        px = list(im.crop((0, int(h * 0.3), w, int(h * 0.7))).getdata())
        bright = sum(1 for v in px if v > 180)     # 数字笔画
        mid = sum(1 for v in px if 25 < v < 70)    # 卡片底
        dark = sum(1 for v in px if v < 16)        # 背景
        check("renders digits", bright > 3000, f"bright={bright}")
        check("renders cards", mid > 5000, f"card_pixels={mid}")
        check("has background", dark > len(px) * 0.3, f"dark_ratio={dark/len(px):.2f}")

    # ---- 场景 B：常驻启动 → 存活 → 整树杀干净 ----
    env2 = dict(os.environ)
    env2["FLIPCLOCK_AUTOTEST"] = "1"          # 不进全屏、不抢常亮、不写真实配置
    env2["FLIPCLOCK_DURATION"] = "30"
    proc = subprocess.Popen([str(EXE)], env=env2,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(6)
    alive = exe_processes()
    check("packaged exe stays alive", len(alive) >= 1, f"pids={alive}")
    if alive:
        try:
            mem = psutil.Process(alive[0]).memory_info().rss / 1048576
            print(f"  info packaged rss = {mem:.1f} MB")
            check("packaged memory under budget", mem < 150, f"{mem:.1f} MB")
        except psutil.NoSuchProcess:
            pass
    kill_tree()
    leftover = exe_processes()
    check("no orphan process after kill", not leftover, f"leftover={leftover}")

    # onedir 不会往 TEMP 解包（对比 onefile 的 _MEI 残留）
    tmp = Path(os.environ.get("TEMP", "."))
    mei = [d for d in tmp.glob("_MEI*") if d.is_dir() and time.time() - d.stat().st_mtime < 600]
    check("no fresh _MEI temp dirs", len(mei) == 0, f"found={[d.name for d in mei][:3]}")

    print(f"PKG_SMOKE pass={PASS} fail={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
