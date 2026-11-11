# 逻辑单测（offscreen，无 GUI）：时间格式化 / 配置原子写与自愈 / NTP 解析。
# 跑法：py -3.10 -X utf8 _probe/test_logic.py
import os
import struct
import sys
import tempfile
import time as pytime
from datetime import datetime, timedelta
from pathlib import Path

# 必须在 import settings 之前：配置重定向进临时目录
os.environ["FLIPCLOCK_AUTOTEST"] = "1"
_SRC = Path(__file__).resolve().parent.parent / "src"
# 防 stale pyc：同秒改写且字符数不变时 (mtime, size) 校验会误判缓存有效
import shutil as _shutil  # noqa: E402

_shutil.rmtree(_SRC / "__pycache__", ignore_errors=True)
sys.path.insert(0, str(_SRC))

import settings  # noqa: E402
import brightness as brightness_mod  # noqa: E402
from time_source import digits_for, format_date, ms_to_next_tick  # noqa: E402
from ntp_check import NtpChecker, NTP_EPOCH_DELTA  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def test_time_source():
    # 24h
    check("24h mid", digits_for(datetime(2026, 1, 1, 0, 5, 9), True) == ("000509", "AM"))
    check("24h noon", digits_for(datetime(2026, 1, 1, 12, 0, 0), True) == ("120000", "PM"))
    check("24h night", digits_for(datetime(2026, 1, 1, 23, 59, 59), True) == ("235959", "PM"))
    # 12h 边界：0 点 → 12AM；12 点 → 12PM；13 点 → 01PM
    d, ap = digits_for(datetime(2026, 1, 1, 0, 0, 0), False)
    check("12h zero", (d, ap) == ("120000", "AM"), f"{d} {ap}")
    d, ap = digits_for(datetime(2026, 1, 1, 12, 0, 0), False)
    check("12h noon", (d, ap) == ("120000", "PM"), f"{d} {ap}")
    d, ap = digits_for(datetime(2026, 1, 1, 13, 5, 0), False)
    check("12h 13", (d, ap) == ("010500", "PM"), f"{d} {ap}")
    # 日期与星期（2026-11-08 是星期日）
    check("date", format_date(datetime(2026, 11, 8)) == "2026年11月8日 · 星期日",
          format_date(datetime(2026, 11, 8)))
    check("date2", format_date(datetime(2026, 11, 9)) == "2026年11月9日 · 星期一")
    # 秒对齐
    check("tick sec mid", ms_to_next_tick(datetime(2026, 1, 1, 0, 0, 5, 500000), True) == 500)
    check("tick sec edge", ms_to_next_tick(datetime(2026, 1, 1, 0, 0, 5, 0), True) == 1000)
    check("tick min", ms_to_next_tick(datetime(2026, 1, 1, 0, 2, 10, 0), False) == 50000)
    check("tick min edge", ms_to_next_tick(datetime(2026, 1, 1, 0, 2, 59, 999999), False) in (1, 0))


def test_settings():
    f = settings.CONF_FILE
    # 先攒一份旧备份（brightness=77），再写正式数据（55）→ .bak=77, config=55
    old = dict(settings.DEFAULTS)
    old["brightness"] = 77
    settings.save(old)
    data = dict(settings.DEFAULTS)
    data["brightness"] = 55
    data["seconds_mode"] = "small"
    settings.save(data)
    loaded = settings.load()
    check("save/load roundtrip", loaded["brightness"] == 55 and loaded["seconds_mode"] == "small")
    check("bak exists", f.with_name(f.name + ".bak").exists())
    # 损坏自愈：写坏正式文件 → load 应回退 .bak（77）且写回正式文件
    f.write_text("{corrupted", "utf-8")
    healed = settings.load()
    check("corrupt heals from bak", healed["brightness"] == 77, str(healed.get("brightness")))
    check("healed written back", f.exists())
    try:
        import json
        json.loads(f.read_text("utf-8"))
        check("written back is json", True)
    except ValueError:
        check("written back is json", False)
    # 亮度 clamp（下限 2026-11-11 放开到 0：低值合法保留，负数才抬回 0）
    data["brightness"] = 999
    settings.save(data)
    check("brightness clamp high", settings.load()["brightness"] == 100)
    data["brightness"] = 5
    settings.save(data)
    check("brightness low value kept", settings.load()["brightness"] == 5)
    data["brightness"] = -3
    settings.save(data)
    check("brightness clamp low", settings.load()["brightness"] == 0)


def test_brightness():
    lv = brightness_mod.level_at
    # 平台期
    check("night platform 02:00", lv(2.0, 100, 40) == 40, lv(2.0, 100, 40))
    check("night platform 23:30", lv(23.5, 100, 40) == 40, lv(23.5, 100, 40))
    check("day platform 12:00", lv(12.0, 100, 40) == 100, lv(12.0, 100, 40))
    check("day platform 18:00", lv(18.0, 100, 40) == 100, lv(18.0, 100, 40))
    # 过渡中点 = 两档中值（smoothstep(0.5)=0.5）
    check("dawn mid 07:00", lv(7.0, 100, 40) == 70, lv(7.0, 100, 40))
    check("dusk mid 20:00", abs(lv(20.0, 100, 40) - 70) < 1e-6, lv(20.0, 100, 40))
    check("dawn start == night", lv(6.0, 100, 40) == 40, lv(6.0, 100, 40))
    check("dusk start == day", lv(18.5, 100, 40) == 100, lv(18.5, 100, 40))
    # 全天单调：0→12 点非降，12→24 点非增；且始终夹在两档之间
    dawn = [lv(h / 4.0, 100, 40) for h in range(0, 49)]
    check("dawn monotonic", all(b >= a - 1e-9 for a, b in zip(dawn, dawn[1:])), dawn)
    dusk = [lv(h / 4.0, 100, 40) for h in range(48, 97)]
    check("dusk monotonic", all(b <= a + 1e-9 for a, b in zip(dusk, dusk[1:])), dusk)
    check("in range all day", all(40 <= v <= 100 for v in dawn + dusk))
    # 用户把夜间调得比白天亮也成立（相反序关系不炸）
    check("inverted ok", lv(12.0, 40, 90) == 40 and lv(2.0, 40, 90) == 90)
    # 手动模式忽略时间；自动模式忽略手动档位之外的字段
    cfg_manual = {"auto_bright": False, "brightness": 55}
    check("manual fixed at noon", brightness_mod.current_level(cfg_manual, datetime(2026, 1, 1, 12)) == 55)
    check("manual fixed at 3am", brightness_mod.current_level(cfg_manual, datetime(2026, 1, 1, 3)) == 55)
    cfg_auto = {"auto_bright": True, "brightness": 45, "bright_day": 100}
    check("auto noon == day", brightness_mod.current_level(cfg_auto, datetime(2026, 1, 1, 12)) == 100)
    check("auto 3am == night", brightness_mod.current_level(cfg_auto, datetime(2026, 1, 1, 3)) == 45)
    # 脏配置不许炸：缺失/None/超界
    check("missing keys default", brightness_mod.current_level({}, datetime(2026, 1, 1, 12)) == 100)
    check("garbage clamp", brightness_mod.current_level(
        {"auto_bright": False, "brightness": 9999}, datetime(2026, 1, 1, 12)) == 100)
    check("garbage type", brightness_mod.current_level(
        {"auto_bright": True, "bright_day": None, "brightness": "x"},
        datetime(2026, 1, 1, 12)) == 100)
    # 下限放到 0（用户 2026-11-11：白天/夜间都要能「调到最低」，0 = 全黑）
    check("min bound is 0", brightness_mod.BRIGHT_MIN == 0, brightness_mod.BRIGHT_MIN)
    check("zero level allowed", brightness_mod.current_level(
        {"auto_bright": False, "brightness": 0}, datetime(2026, 1, 1, 12)) == 0)
    check("negative clamps to 0", brightness_mod.current_level(
        {"auto_bright": False, "brightness": -5}, datetime(2026, 1, 1, 12)) == 0)
    # 落地方式：只有「能调背光且没被强制压暗」才用真背光
    check("impl auto+ok", brightness_mod.resolve_impl({}, True) == "backlight")
    check("impl auto+no", brightness_mod.resolve_impl({}, False) == "overlay")
    check("impl force overlay", brightness_mod.resolve_impl({"bright_impl": "overlay"}, True) == "overlay")
    check("impl dirty -> auto", brightness_mod.resolve_impl({"bright_impl": 7}, True) == "backlight")
    # 手动档必须是 int：背光写入按整数值判"变没变"，float 会让判定失效
    check("manual level is int", isinstance(
        brightness_mod.current_level({"auto_bright": False, "brightness": 55}), int))
    # alpha 映射
    check("alpha 100 -> 0", brightness_mod.overlay_alpha(100) == 0)
    check("alpha 40 -> 153", brightness_mod.overlay_alpha(40) == 153, brightness_mod.overlay_alpha(40))
    # 重绘频率上界：过渡最快处（黎明爬升段）每分钟 alpha 变化 <= 3 格（肉眼不可见）
    base = datetime(2026, 1, 1, 5, 30)
    alphas = [brightness_mod.overlay_alpha(
        brightness_mod.current_level(cfg_auto, base + timedelta(minutes=m)))
        for m in range(0, 210)]  # 05:30 → 09:00
    steps = [abs(b - a) for a, b in zip(alphas, alphas[1:])]
    check("alpha step <= 3/min in ramp", max(steps) <= 3, f"max={max(steps)}")
    repaints = sum(1 for s in steps if s)
    check("ramp repaints <= 70 in 3.5h", repaints <= 70, f"repaints={repaints}")


def test_ntp_parse():
    # 构造一个 48 字节响应：transmit = t0 + 10（服务器比本机快 10s，往返对称）
    t0 = pytime.time() - 100.0
    t1 = t0 + 0.05
    server_unix = t0 + 10.0
    secs = int(server_unix) + NTP_EPOCH_DELTA
    frac = int((server_unix - int(server_unix)) * 2**32)
    pkt = b"\x24" + b"\x00" * 39 + struct.pack("!II", secs, frac)
    off = NtpChecker.parse_ntp_transmit(pkt, t0, t1)
    expect = server_unix - (t0 + t1) / 2.0
    check("ntp offset", off is not None and abs(off - expect) < 1e-6, f"{off} vs {expect}")
    # 短包
    check("ntp short pkt", NtpChecker.parse_ntp_transmit(b"\x00" * 10, t0, t1) is None)
    # 零时间戳
    pkt0 = b"\x24" + b"\x00" * 47
    check("ntp zero ts", NtpChecker.parse_ntp_transmit(pkt0, t0, t1) is None)


def test_snapshot_threadsafe():
    c = NtpChecker()
    c._publish("ok", 0.2, "test")
    snap = c.snapshot()
    check("snapshot fields",
          snap["status"] == "ok" and snap["offset_s"] == 0.2 and snap["server"] == "test"
          and snap["last_check"] is not None, str(snap))


if __name__ == "__main__":
    test_time_source()
    test_settings()
    test_brightness()
    test_ntp_parse()
    test_snapshot_threadsafe()
    print(f"LOGIC_TESTS pass={PASS} fail={FAIL}")
    sys.exit(1 if FAIL else 0)
