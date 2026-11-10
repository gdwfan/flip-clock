# AUTOTEST 截图冒烟：逐场景起真实进程（真 windows 平台渲染），
# 收集 PNG 后用 Pillow 做像素级断言。串行跑（单实例锁）。
#
# 注意：`auto_night_backlight` 场景会**真的**把本机背光调到夜间档约 5 秒
# （FLIPCLOCK_ALLOW_BRIGHT=1 才允许），退出时程序自会还原；脚本另有一项断言专查
# 「退出后系统亮度回到原值」，万一没还原就当场自愈写回。
# 跑法：py -3.10 -X utf8 _probe/smoke.py
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "_probe" / "shots"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "src"))
import clock_widget as cw  # noqa: E402  只读布局常量，用于推导期望值（不创建 QApplication）

# 防 stale pyc：同秒改写且字符数不变时，Python 的 (mtime, size) 校验会误判
# 缓存有效，导致验证跑的是上一版字节码（假绿/假红）。
shutil.rmtree(ROOT / "src" / "__pycache__", ignore_errors=True)

SCENES = [
    ("default", {}),
    ("fmt12", {"fmt24": False}),
    ("nosec", {"seconds_mode": "off"}),
    ("secsmall", {"seconds_mode": "small"}),
    ("nodate_dim", {"show_date": False, "brightness": 45, "auto_bright": False}),
    ("noanim", {"flip_anim": False}),
]

PASS = 0
FAIL = 0


def run_scene(tag, cfg, env_extra=None):
    png = OUT / f"{tag}.png"
    env = dict(os.environ)
    env["FLIPCLOCK_AUTOTEST"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"  # 子进程不留 __pycache__，也不读旧缓存
    env["FLIPCLOCK_SCREENSHOT"] = str(png)
    env["FLIPCLOCK_CFG"] = json.dumps(cfg)
    env.pop("FLIPCLOCK_PANEL", None)
    env.pop("FLIPCLOCK_TOAST", None)
    env.pop("FLIPCLOCK_FREEZE_ANIM", None)
    if env_extra:
        env.update(env_extra)
    r = subprocess.run(
        [sys.executable, "-X", "utf8", str(ROOT / "src" / "main.py")],
        cwd=str(ROOT), env=env, timeout=60,
        capture_output=True, encoding="utf-8", errors="replace",
    )
    ok = r.returncode == 0 and png.exists()
    if not ok:
        so = (r.stdout or "")[-400:]
        se = (r.stderr or "")[-400:]
        print(f"  [{tag}] exit={r.returncode} stdout={so} stderr={se}")
    return ok, png


def band_width(png, y_frac=0.5):
    """横向扫描时钟行中心：返回非背景像素的横向跨度（像素数）。"""
    from PIL import Image
    im = Image.open(png).convert("L")
    w, h = im.size
    y = int(h * y_frac)
    px = im.load()
    xs = [x for x in range(w) if px[x, y] > 22]  # 背景约 6-10，卡片 27+
    return (max(xs) - min(xs)) if xs else 0


def mean_lum(png):
    from PIL import Image
    im = Image.open(png).convert("L")
    im.thumbnail((256, 160))
    px = list(im.getdata())
    return sum(px) / len(px)


def bg_lum(png, k):
    """左下角纯背景小块的灰度（逻辑坐标 40..300 × h-120..h-60）。

    为什么不用整帧平均亮度判定"有没有压暗"：两次独立截图抓到的秒数、冒号闪烁状态不同，
    整帧均值有 3~6% 的自然波动，而叠加 alpha 在"白天档"与"100%"之间的差别也就这么点
    → 断言会随机红（2026-11-10 实测：同一条判据 5 次跑里红 1 次）。
    纯背景区块没有时间元素，灰度只由叠加 alpha 决定，是确定性判据。
    """
    from PIL import Image
    im = Image.open(png).convert("L")
    w, h = im.size
    box = (int(40 * k), int(h - 120 * k), int(300 * k), int(h - 60 * k))
    px = list(im.crop(box).getdata())
    return sum(px) / len(px)


def region_diff(png_a, png_b, box):
    """两个 PNG 在同一区域的逐像素平均灰度差。"""
    from PIL import Image
    a = Image.open(png_a).convert("L").crop(box)
    b = Image.open(png_b).convert("L").crop(box)
    pa, pb = list(a.getdata()), list(b.getdata())
    return sum(abs(x - y) for x, y in zip(pa, pb)) / max(1, len(pa))


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name} {detail}")


def sys_bright():
    """读系统真实背光（本机不支持则 None）。"""
    try:
        import screen_bright
        return screen_bright.get()
    except Exception:  # noqa: BLE001
        return None


def screen_px():
    """主屏物理像素尺寸（不引 Qt，直接问 Windows）。"""
    import ctypes
    u = ctypes.windll.user32
    return u.GetSystemMetrics(0), u.GetSystemMetrics(1)


def main():
    global PASS, FAIL
    from PIL import Image

    # 前置条件：整套像素断言以「AUTOTEST 窗口 1280x800」为基准（k = 宽/1280）。
    # 屏幕比它小 → 窗口被钳住 → k 失真 → 断言全乱。此时明确 SKIP（退出码 2），
    # 而不是抛一堆假红让人以为渲染坏了。
    sw, sh = screen_px()
    if sw < 1280 or sh < 800:
        print(f"SMOKE SKIP: 主屏 {sw}x{sh} 小于基准 1280x800，像素断言不成立；"
              f"请在 ≥1280x800 的机器上跑（本探针不进 CI）")
        return 2

    sys_before = sys_bright()
    # 场景截图
    results = {}
    for tag, cfg in SCENES:
        ok, png = run_scene(tag, cfg)
        check(f"scene {tag} runs", ok)
        if ok:
            results[tag] = png
    # 面板 / 动画冻结 / toast / 自动亮度三档（伪小时固定时段，白 100 夜 40）
    AUTO_CFG = {"auto_bright": True, "bright_day": 100, "brightness": 40}
    for tag, extra, cfg in [
        ("panel", {"FLIPCLOCK_PANEL": "1"}, {}),
        ("panel_auto_off", {"FLIPCLOCK_PANEL": "1"},
         {"auto_bright": False, "brightness": 70}),
        ("anim_freeze", {"FLIPCLOCK_FREEZE_ANIM": "0.5"}, {"flip_anim": True}),
        ("toast", {"FLIPCLOCK_TOAST": "1"}, {}),
        ("auto_night", {"FLIPCLOCK_FAKE_HOUR": "23"}, AUTO_CFG),
        ("auto_dusk", {"FLIPCLOCK_FAKE_HOUR": "20"}, AUTO_CFG),
        ("auto_day", {"FLIPCLOCK_FAKE_HOUR": "13"}, AUTO_CFG),
        # 真背光档：允许子进程写系统亮度 → 画面不该再叠黑（像素应和白天一样亮）
        ("auto_night_backlight", {"FLIPCLOCK_FAKE_HOUR": "23",
                                  "FLIPCLOCK_ALLOW_BRIGHT": "1"}, AUTO_CFG),
    ]:
        ok, png = run_scene(tag, cfg, extra)
        check(f"scene {tag} runs", ok)
        if ok:
            results[tag] = png

    if FAIL:
        print(f"SMOKE aborted: {FAIL} scene failures")
        sys.exit(1)

    # ---- 像素断言（截图是物理像素：125% 缩放 → 逻辑 1280x800 得 1600x1000）----
    im = Image.open(results["default"])
    w, h = im.size
    k = w / 1280.0  # 逻辑→物理缩放系数
    check("size aspect", abs(w / h - 1.6) < 0.01 and w >= 1280, f"{im.size} k={k:.2f}")

    bw_full = band_width(results["default"])
    bw_nosec = band_width(results["nosec"])

    def layout_of(width, height, mode):
        """按布局规则推导 (卡高, 卡组总宽, 卡宽, 冒号宽, 间距)。"""
        scale = cw.SECONDS_SCALE[mode]
        n_sec = 0 if mode == "off" else 2
        n_colon = 1 if mode == "off" else 2
        elems = 4 + n_sec + n_colon
        coeff = (4 * cw.CARD_W_RATIO + n_sec * cw.CARD_W_RATIO * scale
                 + n_colon * cw.COLON_W_RATIO + (elems - 1) * cw.GAP_RATIO)
        card_h = max(cw.CARD_H_MIN,
                     min(cw.CARD_H_MAX, height * cw.CARD_H_VH, width * cw.CARD_H_FIT / coeff))
        return (card_h, coeff * card_h, card_h * cw.CARD_W_RATIO,
                card_h * cw.COLON_W_RATIO, card_h * cw.GAP_RATIO)

    card_h_big, band_big, card_w, colon_w, gap = layout_of(1280, 800, "big")
    expect_full = band_big * k
    check("clock band width matches constants", abs(bw_full - expect_full) < 40 * k,
          f"band={bw_full} expect={expect_full:.0f} k={k:.2f}")
    check("clock band covers >=90% width", bw_full > 0.90 * w,
          f"{bw_full / w * 100:.1f}% of {w}")
    # 关秒后卡组仍占满宽度（卡高吃掉腾出的空间），但数字必须显著变高
    check("no-seconds keeps band full-width", bw_nosec > 0.90 * w,
          f"nosec={bw_nosec / w * 100:.1f}% of {w}")

    # 冒号圆点的亮度必须与数字一致（用户 2026-11-10：「闪动的亮度不够，要跟时:分:秒一样」）。
    # 用无秒场景量——那里冒号常亮，不受 1Hz 闪烁相位影响，判据确定。
    def strip_peak(png, box):
        return max(Image.open(png).convert("L").crop(box).getdata())

    def colon_band(mode):
        """按指定档位的布局算出 (冒号中心 x, 时钟行 y0..y1)，逻辑像素。

        必须按各场景自己的档位算：off 与 big 的卡高不同，用错档位的坐标去采样，
        量到的就是邻卡边缘而不是冒号（2026-11-10 自己踩到，量出 45 以为是"半亮灰"）。
        """
        ch, band, cwd, colw, gp = layout_of(1280, 800, mode)
        x_start = (1280 - band) / 2
        cx = x_start + 2 * (cwd + gp) + colw / 2
        dpx = min(42.0, max(16.0, 1280 * cw.DATE_VW))
        grp = ch + ch * cw.DATE_GAP_RATIO + dpx * 1.6
        y_top = (800 - grp) / 2
        return cx, y_top, ch

    def peak_at(png, cx, y_top, ch):
        return strip_peak(png, (int((cx - 8) * k), int(y_top * k),
                                int((cx + 8) * k), int((y_top + ch) * k)))

    cx_off, y_off, ch_off = colon_band("off")
    cx_big, y_big, ch_big = colon_band("big")
    band_off = layout_of(1280, 800, "off")[1]
    x_card = (1280 - band_off) / 2  # 首张小时卡的左边界
    peak_colon = peak_at(results["nosec"], cx_off, y_off, ch_off)
    peak_digit = strip_peak(results["nosec"], (int(x_card * k), int(y_off * k),
                                               int((x_card + 180) * k), int((y_off + ch_off) * k)))
    check("colon dot as bright as digits", abs(peak_colon - peak_digit) <= 4,
          f"colon={peak_colon} digit={peak_digit}")
    # 闪烁档：只能出现「亮=与数字同亮」或「暗≈熄灭」，不许再是中间那种半亮灰
    peak_colon_blink = peak_at(results["default"], cx_big, y_big, ch_big)
    check("blinking colon is on-or-off, no mid gray",
          peak_colon_blink >= peak_digit - 4 or peak_colon_blink <= 40,
          f"colon={peak_colon_blink} digit={peak_digit}")

    def glyph_span(png, x0f, x1f):
        """区域扫描：返回亮像素（数字笔画）的竖直跨度。"""
        im2 = Image.open(png).convert("L")
        ww, hh = im2.size
        pxx = im2.load()
        ys = [y for y in range(hh)
              for x in range(int(ww * x0f), int(ww * x1f), 2) if pxx[x, y] > 180]
        return (max(ys) - min(ys)) if ys else 0

    span_big = glyph_span(results["default"], 0.26, 0.31)
    span_off = glyph_span(results["nosec"], 0.26, 0.31)
    span_small = glyph_span(results["secsmall"], 0.26, 0.31)
    check("no-seconds digits are much taller", span_off > span_big * 1.25,
          f"big={span_big} off={span_off} (+{span_off / max(1, span_big) * 100 - 100:.0f}%)")
    check("small-seconds digits taller than big", span_small > span_big * 1.02,
          f"big={span_big} small={span_small}")

    # dim 场景整体更暗
    check("dim darker", mean_lum(results["nodate_dim"]) < mean_lum(results["default"]) * 0.75,
          f"dim={mean_lum(results['nodate_dim']):.1f} def={mean_lum(results['default']):.1f}")

    # 自动亮度：用纯背景块的灰度判定（与叠加 alpha 一一对应，不受秒数/冒号影响）
    bg_def = bg_lum(results["default"], k)
    bg_night = bg_lum(results["auto_night"], k)
    bg_dusk = bg_lum(results["auto_dusk"], k)
    bg_day = bg_lum(results["auto_day"], k)
    check("auto night dimmed", bg_night < bg_def * 0.55,
          f"night={bg_night:.2f} def={bg_def:.2f}")
    check("auto day not dimmed", bg_day >= bg_def - 1.5,
          f"day={bg_day:.2f} def={bg_def:.2f}")
    check("auto dusk between night and day", bg_night < bg_dusk < bg_day,
          f"night={bg_night:.2f} dusk={bg_dusk:.2f} day={bg_day:.2f}")
    lum_def = mean_lum(results["default"])
    lum_night = mean_lum(results["auto_night"])

    # 真背光档：画面不叠黑（亮度交给硬件），背景块应与未压暗时一致
    if "auto_night_backlight" in results:
        bg_bl = bg_lum(results["auto_night_backlight"], k)
        check("backlight mode paints no overlay", bg_bl > bg_day * 0.9,
              f"backlight={bg_bl:.2f} day={bg_day:.2f} overlay_night={bg_night:.2f}")
        # 退出还原：子进程自己的 _cleanup 必须把系统亮度写回原值
        sys_after = sys_bright()
        if sys_before is not None and sys_after is not None:
            check("system backlight restored on exit", sys_after == sys_before,
                  f"{sys_before}%→{sys_after}%")
            if sys_after != sys_before:
                try:
                    import screen_bright
                    screen_bright.set(sys_before)  # 没还原就当场自愈，别把开发机留在暗档
                    print(f"  (自愈：背光已写回 {sys_before}%)")
                except Exception:  # noqa: BLE001
                    pass
        else:
            print("  (本机不支持读背光，跳过还原断言)")

    # 面板区域（逻辑 16,62 - 356,530）在 panel 与 default 之间应有显著差异
    box = (int(16 * k), int(62 * k), int(356 * k), int(530 * k))
    d = region_diff(results["panel"], results["default"], box)
    check("panel renders", d > 3.0, f"diff={d:.2f}")

    # toast 场景：底部中央有 toast 文本像素
    im_t = Image.open(results["toast"]).convert("L")
    px_t = im_t.load()
    w, h = im_t.size
    region = [px_t[x, y] for x in range(int(w / 2 - 260 * k), int(w / 2 + 260 * k), 4)
              for y in range(int(h - 110 * k), int(h - 60 * k), 4)]
    check("toast visible", sum(1 for v in region if v > 60) > 10,
          f"bright={sum(1 for v in region if v > 60)}")

    # 数字饱满度：时钟行内的数字笔画（亮像素）总量，随字号/字重提升而增加
    im_d = Image.open(results["default"]).convert("L")
    y0, y1 = int(h * 0.30), int(h * 0.58)
    strokes = sum(1 for v in im_d.crop((0, y0, w, y1)).getdata() if v > 180)
    check("digits bold enough (stroke pixels)", strokes > 40000, f"strokes={strokes}")

    # 动画冻结：秒卡区域应有翻页中间态（对比 noanim 静止态差异显著）
    def region_var(png, box):
        im2 = Image.open(png).convert("L").crop(box)
        vals = list(im2.getdata())
        m = sum(vals) / len(vals)
        return m, sum((v - m) ** 2 for v in vals) / len(vals)

    # 秒卡带（最后两张卡所在区，按常量推算）
    xs = int((w - expect_full) / 2 + 5 * card_w * k + 2 * colon_w * k + 6 * gap * k)
    sbox = (xs, int(h * 0.32), int(xs + card_w * k * 1.9), int(h * 0.55))
    m_a, v_a = region_var(results["anim_freeze"], sbox)
    m_n, v_n = region_var(results["noanim"], sbox)
    check("anim midstate differs", abs(v_a - v_n) > 5 or abs(m_a - m_n) > 2,
          f"anim={v_a:.1f}/{m_a:.1f} static={v_n:.1f}/{m_n:.1f}")

    print(f"SMOKE pass={PASS} fail={FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
