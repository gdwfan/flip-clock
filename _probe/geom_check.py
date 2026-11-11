# 几何诊断：直接读 ClockWidget 的布局产物，验证时钟行水平居中与尺寸推导。
# 跑法：py -3.10 -X utf8 _probe/geom_check.py
import os
import shutil
import sys
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "windows"
os.environ["FLIPCLOCK_AUTOTEST"] = "1"

_SRC = Path(__file__).resolve().parent.parent / "src"
# 防 stale pyc：改过源码后若 (mtime, size) 与缓存相同（同秒改写且字符数一致），
# Python 会加载旧字节码 → 验证跑的是上一版逻辑（假绿/假红）。
# 2026-11-08 反向验证时真实踩到，故所有验证脚本导入前一律清缓存。
shutil.rmtree(_SRC / "__pycache__", ignore_errors=True)
sys.path.insert(0, str(_SRC))

from PySide6.QtWidgets import QApplication  # noqa: E402
import settings as settings_mod  # noqa: E402
import clock_widget as cw  # noqa: E402
from clock_widget import ClockWidget  # noqa: E402
from settings_panel import SettingsPanel  # noqa: E402

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
win = ClockWidget(cfg, lambda: {"status": "init", "offset_s": None})

# 基准尺寸 1280x800，但**不能假设屏幕装得下**：桌面比它小时窗口会被钳住
# （CI runner 是 1024x768 → 实测被钳成 1028x749）。下面所有断言都从实际 W/H 推导，
# 只有这一条尺寸断言，所以按可用区取 min 即可，窄屏机器不再假失败。
scr = app.primaryScreen().availableGeometry()
want_w, want_h = min(1280, scr.width()), min(800, scr.height())
win.resize(want_w, want_h)
win.show()
app.processEvents()
win._relayout()

W = win.width()
print(f"screen={scr.width()}x{scr.height()} want={want_w}x{want_h} "
      f"logical size={W}x{win.height()} dpr={win.devicePixelRatioF()}")
check(f"logical width is {want_w}", W == want_w, f"got {W}（屏 {scr.width()}x{scr.height()}）")

rects = win._rects
print("card rects:")
for slot, r in rects.items():
    print(f"  {slot}: x={r.left():.1f} w={r.width():.1f} y={r.top():.1f} h={r.height():.1f}")
for i, r in enumerate(win._colon_rects):
    print(f"  colon{i}: x={r.left():.1f} w={r.width():.1f}")

def expect_layout(width, height, mode):
    """按 clock_widget 的规则推算 (卡高, 卡组总宽)。独立复刻公式才能校验实现。"""
    scale = cw.SECONDS_SCALE[mode]
    n_sec = 0 if mode == "off" else 2
    n_colon = 1 if mode == "off" else 2
    elems = 4 + n_sec + n_colon
    coeff = (4 * cw.CARD_W_RATIO + n_sec * cw.CARD_W_RATIO * scale
             + n_colon * cw.COLON_W_RATIO + (elems - 1) * cw.GAP_RATIO)
    card_h = max(cw.CARD_H_MIN,
                 min(cw.CARD_H_MAX, height * cw.CARD_H_VH, width * cw.CARD_H_FIT / coeff))
    return card_h, coeff * card_h


left = min(r.left() for r in list(rects.values()) + win._colon_rects)
right = max(r.right() for r in list(rects.values()) + win._colon_rects)
band = right - left
center = (left + right) / 2.0
print(f"band=({left:.1f} .. {right:.1f}) width={band:.1f} center={center:.1f} screen_center={W/2:.1f}")

expect_h, expect_band = expect_layout(W, win.height(), "big")

check("band centered", abs(center - W / 2) < 2.0, f"off by {center - W/2:.1f}px")
check("band width matches constants", abs(band - expect_band) < 1.5,
      f"band={band:.1f} expect={expect_band:.1f}")
check("band uses >=90% of width", band > W * 0.90, f"{band/W*100:.1f}% of screen")
check("band fits screen", left >= 0 and right <= W, f"{left:.1f}..{right:.1f}")
check("card h matches constants", abs(win._card_h - expect_h) < 0.1, f"{win._card_h:.1f}")

# 字形必须放得进卡片：宽体字靠横向压缩达成「窄卡大字」
font = cw._clock_font(win._card_h)
from PySide6.QtGui import QFont, QFontMetrics  # noqa: E402
fm = QFontMetrics(font)
card_w = win._card_h * cw.CARD_W_RATIO
widest_nat = max(fm.horizontalAdvance(ch) for ch in "0123456789")
sx = min(1.0, (card_w * cw.GLYPH_FILL) / widest_nat)
check("digit fits card after squeeze", widest_nat * sx < card_w * 0.99,
      f"rendered={widest_nat * sx:.1f} card_w={card_w:.1f} sx={sx:.2f}")
# 压缩过狠会让竖笔画明显细于横笔画（字形失衡），下限 0.75
check("squeeze keeps strokes balanced", sx >= 0.75, f"sx={sx:.2f}")
cap = fm.capHeight()
check("glyph height fills card", cap > win._card_h * 0.6,
      f"cap={cap} card_h={win._card_h:.1f} ({cap/win._card_h*100:.0f}%)")

# 纵向上限：时钟行 + 日期行必须放得进屏高（用户要"再高些"，得确认没顶出屏）。
# 直接读布局产物 _date_rect（单一事实源），不复述字号推导公式，防两处口径漂移。
date_h = win._date_rect.height()
group_h = win._card_h + win._card_h * cw.DATE_GAP_RATIO + date_h
check("clock+date fit vertically", group_h < win.height() * 0.92,
      f"group={group_h:.1f} screen_h={win.height()} ({group_h/win.height()*100:.0f}%)")

# 日期行横向顶满（用户 2026-11-11：按横向宽度做到最大）：最宽日期串在生效字号下的
# 渲染宽度必须贴着 屏宽×DATE_FIT（容差 3%，吸收迭代取整的残余误差）。
fnt = QFont("Segoe UI")
fnt.setWeight(QFont.Weight.Medium)
fnt.setPixelSize(int(date_h / 1.6))
fnt.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, W * 0.006)
date_adv = QFontMetrics(fnt).horizontalAdvance(cw._DATE_PROBE)
check("date row fills width", abs(date_adv - W * cw.DATE_FIT) < W * cw.DATE_FIT * 0.03,
      f"adv={date_adv:.0f} target={W * cw.DATE_FIT:.0f} px={date_h / 1.6:.0f}")

# 元素顺序：冒号必须落在「时|分」与「分|秒」的边界上。
# 只验总宽/居中会漏掉「冒号插进小时内部」这类错位（2026-11-08 真实踩过）。
c0, c1 = win._colon_rects[0], win._colon_rects[1]
check("colon0 between h2 and m1",
      rects["h2"].right() < c0.left() and c0.right() < rects["m1"].left(),
      f"h2.r={rects['h2'].right():.1f} c0={c0.left():.1f}..{c0.right():.1f} m1.l={rects['m1'].left():.1f}")
check("colon1 between m2 and s1",
      rects["m2"].right() < c1.left() and c1.right() < rects["s1"].left(),
      f"m2.r={rects['m2'].right():.1f} c1={c1.left():.1f}..{c1.right():.1f} s1.l={rects['s1'].left():.1f}")
gap_h1_h2 = rects["h2"].left() - rects["h1"].right()
span_h2_m1 = rects["m1"].left() - rects["h2"].right()
gap_m1_m2 = rects["m2"].left() - rects["m1"].right()
check("no colon inside hours", span_h2_m1 > gap_h1_h2 + 30,
      f"h1|h2={gap_h1_h2:.1f} vs h2|m1={span_h2_m1:.1f}")
check("no colon inside minutes", span_h2_m1 > gap_m1_m2 + 30,
      f"m1|m2={gap_m1_m2:.1f} vs h2|m1={span_h2_m1:.1f}")

# 无秒布局
cfg2 = dict(cfg)
cfg2["seconds_mode"] = "off"
win.set_config(cfg2)
r2 = win._rects
l2 = min(r.left() for r in list(r2.values()) + win._colon_rects)
rt2 = max(r.right() for r in list(r2.values()) + win._colon_rects)
c2 = (l2 + rt2) / 2
check("nosec centered", abs(c2 - W / 2) < 2.0, f"off by {c2 - W/2:.1f}px")
check("nosec 4 cards", len(r2) == 4, str(list(r2.keys())))
check("nosec 1 colon", len(win._colon_rects) == 1)
if len(win._colon_rects) == 1:
    cc = win._colon_rects[0]
    check("nosec colon between h2 and m1",
          r2["h2"].right() < cc.left() and cc.right() < r2["m1"].left(),
          f"h2.r={r2['h2'].right():.1f} c={cc.left():.1f}..{cc.right():.1f} m1.l={r2['m1'].left():.1f}")

h_off, _ = expect_layout(W, win.height(), "off")
check("no-seconds mode grows digits vs big", h_off > expect_h * 1.4,
      f"off_card_h={h_off:.0f} big_card_h={expect_h:.0f} (+{(h_off/expect_h-1)*100:.0f}%)")

# 秒小号档：秒卡按比例缩小，且与时钟行中线对齐
cfg3 = dict(cfg)
cfg3["seconds_mode"] = "small"
win.set_config(cfg3)
r3 = win._rects
h_small, _ = expect_layout(W, win.height(), "small")
check("small mode: 6 cards", len(r3) == 6, str(list(r3.keys())))
check("small mode: main card grows vs big", win._card_h > expect_h * 1.05,
      f"card_h={win._card_h:.1f} vs big={expect_h:.1f}")
sec_scale = cw.SECONDS_SCALE["small"]
check("small mode: sec card scaled",
      abs(r3["s1"].height() - win._card_h * sec_scale) < 0.6,
      f"sec_h={r3['s1'].height():.1f} expect={win._card_h * sec_scale:.1f}")
check("small mode: sec card centered with main row",
      abs(r3["s1"].center().y() - r3["h1"].center().y()) < 0.6,
      f"sec_cy={r3['s1'].center().y():.1f} main_cy={r3['h1'].center().y():.1f}")
check("small mode matches formula", abs(win._card_h - h_small) < 0.1,
      f"card_h={win._card_h:.1f} expect={h_small:.1f}")

# ---- 渲染像素级验证：数字必须完整落在卡片内，不得触边/被裁 ----
# 白盒算不出「字形在卡片内的实际落位」（受字体 ascent/descent 影响），
# 必须真渲染一帧再扫描亮像素范围。grab() 会强制整窗重绘，这里正合适。
from PySide6.QtGui import qGray  # noqa: E402

win.resize(want_w, want_h)
cfg_secs = dict(cfg)
cfg_secs["seconds_mode"] = "big"    # 前面的用例把秒关了，恢复 6 张卡
cfg_secs["flip_anim"] = False      # 关动画：要测静态落位，动画中间帧会混入旧数字
win.set_config(cfg_secs)
win.show()
app.processEvents()
win._last_digits = ""
win._tick()
app.processEvents()
shot = win.grab().toImage()
dpr = win.devicePixelRatioF()
print(f"render check: shot={shot.width()}x{shot.height()} dpr={dpr}")

worst_gap_top, worst_gap_bot = 1e9, 1e9
for slot in ("h1", "h2", "m1", "m2", "s1", "s2"):
    r = win._rects[slot]
    # 扫描卡片内的全部亮像素（不能用单条中心线：某些字形如 "1" 的笔画不在正中）
    x0, x1 = int((r.left() + r.width() * 0.06) * dpr), int((r.right() - r.width() * 0.06) * dpr)
    y0, y1 = int((r.top() - 8) * dpr), int((r.bottom() + 8) * dpr)
    ys = [y for y in range(max(0, y0), min(shot.height(), y1), 2)
          for x in range(x0, x1, 2)
          if qGray(shot.pixel(x, y)) > 180]
    if not ys:
        check(f"digit rendered in {slot}", False, "no bright pixels")
        continue
    glyph_top, glyph_bot = min(ys) / dpr, max(ys) / dpr
    gap_top, gap_bot = glyph_top - r.top(), r.bottom() - glyph_bot
    worst_gap_top, worst_gap_bot = min(worst_gap_top, gap_top), min(worst_gap_bot, gap_bot)
    print(f"  {slot}: glyph {glyph_top - r.top():.1f}..{glyph_bot - r.top():.1f} "
          f"gap_top={gap_top:.1f} gap_bot={gap_bot:.1f} (card {r.height():.1f})")

check("digit not clipped (gap >= 2px both sides)",
      worst_gap_top >= 2.0 and worst_gap_bot >= 2.0,
      f"worst_top={worst_gap_top:.1f} worst_bot={worst_gap_bot:.1f}")
check("digit fills card height (max gap <= 25% card)",
      max(worst_gap_top, worst_gap_bot) < win._card_h * 0.25,
      f"max_gap={max(worst_gap_top, worst_gap_bot):.1f} card_h={win._card_h:.1f}")
check("digit vertically centered (gap diff <= 5% card)",
      abs(worst_gap_top - worst_gap_bot) < win._card_h * 0.05,
      f"top={worst_gap_top:.1f} bot={worst_gap_bot:.1f} card_h={win._card_h:.1f}")
win.hide()

print(f"GEOM_TESTS pass={PASS} fail={FAIL}")
sys.exit(1 if FAIL else 0)
