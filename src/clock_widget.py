"""翻页时钟主控件：单 QWidget 全屏自绘（方案 §3.2）。

资源策略：字形位图缓存（0-9 共 10 张）；跳秒只 update 变化卡片的矩形；
翻页动画只重绘目标卡片；静止期零重绘零定时器回调。

视觉规格（方案 §4）：Bahnschrift SemiBold；卡高 = 屏宽 15.5%（66-240 clamp）；
全部尺寸由卡高派生，无独立魔法数。
"""
import math
import os
import time
from datetime import datetime

from PySide6.QtCore import Qt, QPointF, QRectF, QTimer, Signal, QVariantAnimation
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontInfo,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QWidget

import ntp_check
import brightness as brightness_mod
import screen_bright
from time_source import digits_for, format_date, ms_to_next_tick

# 调色板（与设计稿一致）
BG = QColor("#060608")
CARD_TOP = QColor("#2c2c33")
CARD_BOT = QColor("#1b1b20")
FG = QColor("#f6f6f8")
DIM = QColor("#7f7f88")
DATE_C = QColor("#9a9aa5")
COLON_C = QColor("#55555e")  # 仅用于徽标/状态文字（别拿来画冒号，见下）
# 冒号圆点：点亮态与数字同亮（用户 2026-11-10 明确要求「闪动的亮度要跟时:分:秒一样」），
# 熄灭态保持深色以留出呼吸对比
COLON_ON = FG
COLON_OFF = QColor("#1a1a20")
ACCENT = QColor("#7ec97e")
WARN = QColor("#d4b05a")

BTN_SIZE = 44  # 触屏热区（方案 §3.4）
FLIP_MS = 350  # 翻页动画时长

# ---------------------------------------------------------------------------
# 布局与字体参数（单一事实源：改视觉只动这里，_relayout/_build_glyphs/日期共用）
# 全部以「卡高」为基准派生，卡高又以屏宽派生 —— 换屏幕尺寸整体等比缩放。
# ---------------------------------------------------------------------------
CARD_H_MIN = 66.0      # 极窄屏下限
CARD_H_MAX = 400.0     # 上限（关秒模式下能到 ~397）
CARD_H_FIT = 0.95      # 卡组最多占屏宽的比例（横向硬约束）
CARD_H_VH = 0.55       # 卡高最多占屏高比例（纵向护栏，留出日期与边距）
CARD_W_RATIO = 0.70    # 卡宽/卡高（比字形自然宽度窄，配合横向压缩）
COLON_W_RATIO = 0.14   # 冒号列宽/卡高
GAP_RATIO = 0.03       # 卡间距/卡高（压紧，翻页钟本就该密排）
GLYPH_SIZE_RATIO = 1.20  # 字号/卡高（字形高度约占卡高 86%）
GLYPH_FILL = 0.96      # 数字宽度占卡宽的目标比例（配合横向压缩实现"窄卡大字"）
FONT_STACK = ("Arial Black", "Arial", "Segoe UI")  # 用户 2026-11-08 选定
DATE_VW = 0.032        # 日期字号 = 屏宽 ×此值
DATE_PX_MIN, DATE_PX_MAX = 16.0, 42.0
DATE_GAP_RATIO = 0.22  # 日期行与时钟行的间距/卡高

# 秒显示三档（用户 2026-11-08 要求「关秒」和「秒小一号」都要）：
#   big   秒与时:分同大（6 卡，卡高 ~259px @1280）
#   small 秒卡缩一号（6 卡，时:分能更大 ~287px，主次分明）
#   off   不显示秒（4 卡，时:分最大 ~397px）
SECONDS_SCALE = {"big": 1.0, "small": 0.68, "off": 0.0}


def _mode_of(cfg: dict) -> str:
    """从配置取秒显示档位（兼容旧 show_seconds 布尔字段）。"""
    mode = cfg.get("seconds_mode")
    if mode in SECONDS_SCALE:
        return mode
    return "big" if cfg.get("show_seconds", True) else "off"


def _clock_font(card_h: float) -> QFont:
    """时钟数字字体：Arial Black（用户选定，最粗最饱满）；缺字时退回 Segoe UI Black。"""
    size = max(1, int(card_h * GLYPH_SIZE_RATIO))
    f = QFont(FONT_STACK[0])
    f.setPixelSize(size)
    if QFontInfo(f).family() != FONT_STACK[0]:
        f = QFont(FONT_STACK[2])
        f.setWeight(QFont.Weight.Black)
        f.setPixelSize(size)
    return f


def _date_px(width: float) -> float:
    """日期字号：屏宽派生（_relayout 与 _draw_date 共用，防两处口径漂移）。"""
    return min(DATE_PX_MAX, max(DATE_PX_MIN, width * DATE_VW))


class _Card:
    """一张翻页卡的状态：当前字符 / 动画中保留的旧字符 / 动画进度。"""

    __slots__ = ("slot", "cur", "prev", "anim_t", "_anim")

    def __init__(self, slot, owner):
        self.slot = slot
        self.cur = "?"
        self.prev = "?"
        self.anim_t = -1.0  # <0 = 无动画
        self._anim = QVariantAnimation(owner)
        self._anim.setDuration(FLIP_MS)
        self._anim.valueChanged.connect(lambda v, s=slot: owner._on_anim_value(s, float(v)))
        self._anim.finished.connect(lambda s=slot: owner._on_anim_done(s))

    def start_flip(self):
        if self._anim.state() == QVariantAnimation.Running:
            self._anim.stop()
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.start()

    def stop_flip(self):
        if self._anim.state() == QVariantAnimation.Running:
            self._anim.stop()
        self.anim_t = -1.0


class ClockWidget(QWidget):
    # 三通道退出全屏（方案 §3.4）：✕ 按钮 / 双击 / Esc（Esc 在 main 的 keyPressEvent 里转发）
    exit_fullscreen_requested = Signal()
    toggle_fullscreen_requested = Signal()
    panel_toggle_requested = Signal()

    def __init__(self, cfg: dict, ntp_status_provider, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._ntp = ntp_status_provider
        self.setMouseTracking(True)
        # 必须显式要焦点，否则收不到 keyPressEvent —— Esc 退出全屏通道会静默失效
        # （QWidget 默认 NoFocus，2026-11-08 复查发现）
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setMinimumSize(320, 240)

        # 布局产物
        self._freeze = 0.0  # AUTOTEST 动画冻结帧
        self._sec_card_h = 0.0  # 秒卡高度（small 档小于主卡）
        self._rects = {}          # slot → QRectF（含卡片）
        self._colon_rects = []    # [QRectF, ...]
        self._ampm_x = 0.0
        self._date_rect = QRectF()
        self._card_h = 0.0
        self._glyphs = {}         # (ch, int(card_h)) → QPixmap
        self._last_digits = ""
        self._last_ap = ""
        self._last_date = ""
        self._colon_bright = True

        # 角落按钮
        self._hover_btn = None    # None / 'exit' / 'gear'
        self._pressed_btn = None  # 按下态（触摸反馈）

        # NTP 徽标内容缓存：内容没变就不重绘，保持静止期零重绘
        self._badge_cache = None

        # 当前生效亮度（自动模式下随时间变化；绘制与重绘判定共用同一个值）
        self._bright_now = brightness_mod.current_level(cfg)
        # 落地方式：真背光（Power API，省电且夜里不刺眼）优先，设备不支持则画面压暗
        self._bright_impl = brightness_mod.resolve_impl(cfg, screen_bright.available())
        self._bright_applied = False
        # 拖滑杆会连打几十次背光写入（每次一个 ACPI 调用，低配平板上很慢）→ 合并成一次
        self._bright_timer = QTimer(self)
        self._bright_timer.setSingleShot(True)
        self._bright_timer.timeout.connect(self._apply_backlight)

        # 窗口化几何是否已准备好（首次退出全屏时设置）
        self._windowed_prepared = False

        # Toast（首次运行手势提示）
        self._toast_until = 0.0

        # 卡片
        self._slots = ["h1", "h2", "m1", "m2", "s1", "s2"]
        self._cards = {s: _Card(s, self) for s in self._slots}

        # 走时驱动：秒对齐 singleShot 链（CoarseTimer 精度足够且不锁 CPU 深度省电态）
        self._tick_timer = QTimer(self)
        self._tick_timer.setSingleShot(True)
        self._tick_timer.timeout.connect(self._tick)
        # NTP 状态轮询（UI 侧拉取，daemon 线程绝不反向碰 UI）
        self._badge_timer = QTimer(self)
        self._badge_timer.timeout.connect(self._poll_ntp)
        self._badge_timer.start(2000)

    # ---------------- 公共接口 ----------------

    def set_config(self, cfg: dict):
        old = self._cfg
        self._cfg = dict(cfg)
        layout_changed = (
            _mode_of(self._cfg) != _mode_of(old)
            or self._cfg.get("show_date", True) != old.get("show_date", True)
            or self._cfg.get("fmt24", True) != old.get("fmt24", True)
        )
        if layout_changed:
            self._glyphs.clear()
            self._relayout()
            self._last_digits = ""
            self._last_ap = ""
            self._last_date = ""
            self._badge_cache = None
        # 关键：配置变更一律全窗重绘（self.update()）。
        # 平时的局部重绘（_update_card_area）只标脏【新】布局的矩形；布局/显隐一变，
        # 旧卡片、旧 AM/PM、旧日期的像素就没有任何脏区去覆盖 → 画面残留错乱，
        # 只有最小化/恢复这类强制 expose 才自愈（2026-11-08 用户真机复现）。
        new_impl = brightness_mod.resolve_impl(self._cfg, screen_bright.available())
        if new_impl != self._bright_impl:
            # 切换落地方式：从背光切走必须先把硬件亮度还给用户，否则「背光已降过 + 再叠一层黑」
            # = 双重变暗，用户会觉得越切越暗（2026-11-10 加切换功能时踩到）
            if self._bright_impl == "backlight":
                screen_bright.restore()
            self._bright_impl = new_impl
            self._bright_applied = False
        self.update()
        self._refresh_brightness(force=True)  # 亮度档位/方式改了要立刻落到位
        self._tick()

    def prepare_windowed(self):
        """从全屏切回窗口时给一个合理尺寸并居中（只生效一次）。

        原先只 showNormal()，恢复的是 Qt 记的 normalGeometry —— 首次往往是全屏尺寸
        或默认小窗，退出全屏后窗口形状不可预期（2026-11-08 复查发现）。
        """
        if self._windowed_prepared:
            return
        scr = self.screen()
        if scr is not None:
            area = scr.availableGeometry()
            w, h = int(area.width() * 0.7), int(area.height() * 0.7)
            self.resize(w, h)
            self.move(area.center().x() - w // 2, area.center().y() - h // 2)
        self._windowed_prepared = True

    def refresh_now(self):
        """WM_TIMECHANGE / 恢复显示等强制刷新。"""
        self._last_digits = ""
        self._last_ap = ""
        self._last_date = ""
        self.update()
        self._tick()

    def show_toast(self, seconds: float = 4.2):
        self._toast_until = time.monotonic() + seconds
        QTimer.singleShot(int(seconds * 1000) + 200, lambda: self.update())
        self.update()

    def freeze_anim_at(self, progress: float):
        """AUTOTEST：把下一跳的翻页动画钉在指定进度（验证中间态）。"""
        self._freeze = progress

    # ---------------- 布局 ----------------

    def _seconds_mode(self) -> str:
        """秒显示档位：big / small / off（兼容旧配置的 show_seconds 布尔值）。"""
        return _mode_of(self._cfg)

    def _relayout(self):
        w, h = float(self.width()), float(self.height())
        if w < 20 or h < 20:
            return
        mode = self._seconds_mode()
        sec_scale = SECONDS_SCALE[mode]
        n_sec = 0 if mode == "off" else 2
        n_colon = 1 if mode == "off" else 2

        # 卡高不再写死比例，而是【按剩余横向空间反算】——这样关掉秒或缩小秒卡后，
        # 时:分会自动吃掉腾出的宽度、整体变大（用户要的"再高一些"）。
        elems = 4 + n_sec + n_colon
        width_coeff = (4 * CARD_W_RATIO
                       + n_sec * CARD_W_RATIO * sec_scale
                       + n_colon * COLON_W_RATIO
                       + (elems - 1) * GAP_RATIO)
        card_h = min(CARD_H_MAX, h * CARD_H_VH, w * CARD_H_FIT / width_coeff)
        card_h = max(CARD_H_MIN, card_h)
        self._card_h = card_h
        self._sec_card_h = card_h * sec_scale
        gap = card_h * GAP_RATIO
        card_w = card_h * CARD_W_RATIO
        colon_w = card_h * COLON_W_RATIO
        sec_w = card_w * sec_scale

        # 元素序列：h1 h2 [：] m1 m2 （[：] s1 s2）
        seq = [("h1", card_w, card_h), ("h2", card_w, card_h), ("c0", colon_w, card_h),
               ("m1", card_w, card_h), ("m2", card_w, card_h)]
        if mode != "off":
            seq += [("c1", colon_w, card_h),
                    ("s1", sec_w, self._sec_card_h), ("s2", sec_w, self._sec_card_h)]
        total_w = sum(e[1] for e in seq) + (len(seq) - 1) * gap
        x = (w - total_w) / 2.0
        row_center = 0.0  # 稍后按 y0 再定

        self._rects.clear()
        self._colon_rects.clear()
        # 垂直：时钟行 + 可选日期行整体居中；秒卡（若小一号）与时钟行中线对齐
        show_date = self._cfg.get("show_date", True)
        date_px = _date_px(w)
        date_gap = card_h * DATE_GAP_RATIO
        date_h = date_px * 1.6
        group_h = card_h + (date_gap + date_h if show_date else 0)
        y0 = (h - group_h) / 2.0
        row_center = y0 + card_h / 2.0

        for slot, ew, eh in seq:
            top = y0 + (card_h - eh) / 2.0 if eh != card_h else y0
            r = QRectF(x, top, ew, eh)
            if slot.startswith("c"):
                self._colon_rects.append(r)
            else:
                self._rects[slot] = r
            x += ew + gap
        self._ampm_x = (w - total_w) / 2.0 - card_h * 0.30
        self._date_rect = QRectF(0, y0 + card_h + date_gap, w, date_h)
        # 注意：不在此清字形缓存——本函数可能从 paintEvent 的兜底路径进入，
        # 清缓存会销毁正在绘制的 QPixmap（QPaintDevice: Cannot destroy...）。
        # 缓存清理只在 resizeEvent / set_config 里做。
        self._build_glyphs()

    def _build_glyphs(self):
        """布局期预渲染 0-9 字形位图（主卡 + 秒小卡两档尺寸）。

        关键：绝不在 paintEvent 里建 QPixmap/QPainter——那会在绘制上下文中
        初始化 DirectWrite 字体引擎，偶发触发 paint 重入与 backing store 异常。

        横向压缩：数字按「卡宽 × GLYPH_FILL」为目标等比压窄（只压不拉），
        使宽体字（Arial Black）也能放进窄卡，从而让卡片可以纵向拉高、数字变大。
        垂直定位：用 baseline 让 cap 区域在卡内居中（行盒居中会让字形整体偏下）。
        """
        if self._card_h <= 1:
            return
        sizes = {int(self._card_h)}
        if getattr(self, "_sec_card_h", 0.0) > 1:
            sizes.add(int(self._sec_card_h))
        for size in sorted(sizes):
            font = _clock_font(size)
            fm = QFontMetrics(font)
            pad = int(size * 0.12)
            h = max(1, int(size * 1.45))
            card_w = size * CARD_W_RATIO
            cap = fm.capHeight()
            baseline = (h + cap) / 2.0
            for ch in "0123456789":
                adv = max(1, fm.horizontalAdvance(ch))
                sx = min(1.0, (card_w * GLYPH_FILL) / adv)
                pw = max(1, int(adv * sx) + pad * 2)
                x_left = (pw - adv) / 2.0
                pm = QPixmap(pw, h)
                pm.fill(Qt.transparent)
                pp = QPainter(pm)
                pp.setFont(font)
                # 以 pixmap 中线为轴横向压缩，保证字形仍居中
                pp.translate(pw / 2.0, 0)
                pp.scale(sx, 1.0)
                pp.translate(-pw / 2.0, 0)
                pp.setPen(QColor(0, 0, 0, 90))
                pp.drawText(QPointF(x_left, baseline + size * 0.02), ch)
                pp.setPen(FG)
                pp.drawText(QPointF(x_left, baseline), ch)
                pp.end()
                self._glyphs[(ch, size)] = pm

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()
        self._glyphs.clear()
        self._last_digits = ""

    def showEvent(self, event):
        super().showEvent(event)
        self._relayout()
        # 拿到焦点，保证 Esc 退出全屏通道可用
        self.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        # 首帧就把亮度落到位：23 点启动的程序不该先满亮闪一下再变暗
        self._refresh_brightness(force=True)
        QTimer.singleShot(0, self._tick)

    # ---------------- 字形缓存 ----------------

    def _glyph(self, ch: str, card_h: float) -> QPixmap:
        key = (ch, int(card_h))
        pm = self._glyphs.get(key)
        if pm is None:
            # 兜底（正常流程由 _build_glyphs 在布局期填好）
            self._build_glyphs()
            pm = self._glyphs.get(key)
        return pm

    # ---------------- 走时 ----------------

    def _tick(self):
        now = datetime.now()
        with_secs = self._seconds_mode() != "off"
        digits, ap = digits_for(now, self._cfg.get("fmt24", True))
        if not with_secs:
            digits = digits[:4]

        if digits != self._last_digits:
            for i, slot in enumerate(self._slots[: len(digits)]):
                ch = digits[i]
                card = self._cards[slot]
                if card.cur != ch:
                    card.prev = card.cur
                    card.cur = ch
                    if self._cfg.get("flip_anim", True):
                        if self._freeze > 0:
                            card.anim_t = self._freeze  # AUTOTEST 冻结帧
                        else:
                            card.start_flip()
                    else:
                        card.stop_flip()
                    self._update_card_area(slot)
            self._last_digits = digits

        if ap != self._last_ap:
            self._last_ap = ap
            self._update_ampm_area()

        date_txt = format_date(now)
        if date_txt != self._last_date:
            self._last_date = date_txt
            self.update(self._date_rect.toAlignedRect())

        # 冒号呼吸（显示秒时 1Hz 硬闪；无秒模式静态）
        if with_secs:
            bright = (now.second % 2) == 0
            if bright != self._colon_bright:
                self._colon_bright = bright
                for r in self._colon_rects:
                    self.update(r.toAlignedRect())

        self._refresh_brightness()
        self._tick_timer.start(max(1, ms_to_next_tick(datetime.now(), with_secs)))

    def _refresh_brightness(self, force: bool = False):
        """按时间/配置重算生效亮度，落到真背光或画面叠加层。

        只在值真变了（或 force 的首次施加）时动作：真背光走 Power API 且**不重绘**
        （不加黑色叠加，画面保持满对比）；画面压暗才需要一次全窗重绘。
        过渡期最快约 2 分钟变一格，平台期（白天/深夜）零动作。
        """
        lv = brightness_mod.current_level(self._cfg)
        if lv == self._bright_now and self._bright_applied and not force:
            return
        self._bright_now = lv
        self._bright_applied = True
        if self._bright_impl == "overlay":
            self.update()
            return
        if force:
            self._apply_backlight()          # 启动/改配置：立刻写一次
        else:
            self._bright_timer.start(150)    # 拖滑杆：合并写入，只落最后一次

    def _apply_backlight(self):
        """写真背光；写失败（设备不支持或驱动拒绝）当场降级为画面压暗，别让调节能力丢失。"""
        if self._bright_impl != "backlight":
            return
        if screen_bright.set(self._bright_now):
            return
        self._bright_impl = "overlay"
        self.update()

    def _on_anim_value(self, slot, v):
        card = self._cards.get(slot)
        if card is not None:
            card.anim_t = v
            self._update_card_area(slot)

    def _on_anim_done(self, slot):
        card = self._cards.get(slot)
        if card is not None:
            card.anim_t = -1.0
            card.prev = card.cur
            self._update_card_area(slot)

    def _update_card_area(self, slot):
        r = self._rects.get(slot)
        if r is not None:
            m = int(self._card_h * 0.12) + 4
            self.update(r.adjusted(-m, -m, m, m).toAlignedRect())

    def _update_ampm_area(self):
        if not self._cfg.get("fmt24", True):
            top = next(iter(self._rects.values())).top() if self._rects else 0
            self.update(int(self._ampm_x - 30), int(top), 70, int(self._card_h))

    # ---------------- NTP 徽标 ----------------

    def _poll_ntp(self):
        if not self._cfg.get("ntp_check", True):
            return
        state = self._badge_state()
        if state != self._badge_cache:  # 内容真变了才重绘
            self._badge_cache = state
            self.update(self._badge_rect().toAlignedRect())

    def _badge_rect(self) -> QRectF:
        return QRectF(self.width() - 320, self.height() - 40, 304, 30)

    def _badge_state(self):
        """返回 (颜色, 文本, 是否已同步)。绘制与重绘判定共用，避免两处口径漂移。"""
        # 开关关闭时必须以「本地时间」呈现，不能沿用上次校时结果
        # （否则关掉网络校时后右下角仍挂着「NTP 已同步」，2026-11-08 复查发现）
        if not self._cfg.get("ntp_check", True):
            return COLON_C, "本地时间", False
        snap = self._ntp() if self._ntp else {"status": "init", "offset_s": None}
        if snap["status"] == "ok" and snap["offset_s"] is not None:
            off = snap["offset_s"]
            if abs(off) >= ntp_check.WARN_OFFSET_S:
                return WARN, f"时间偏差 {off:+.1f}s", False
            return ACCENT, f"NTP 已同步 · 偏差 {off:+.1f}s", True
        return COLON_C, "本地时间", False

    # ---------------- 绘制 ----------------

    def paintEvent(self, event):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing)
            p.fillRect(self.rect(), BG)

            if not self._rects:
                self._relayout()
            if not self._rects:
                return

            # 12 小时制 AM/PM
            if not self._cfg.get("fmt24", True):
                self._draw_ampm(p)

            # 卡片
            for slot in self._rects:
                self._draw_card(p, self._rects[slot], self._cards[slot])

            # 冒号
            for r in self._colon_rects:
                self._draw_colon(p, r)

            # 日期
            if self._cfg.get("show_date", True):
                self._draw_date(p)

            # 角落按钮
            self._draw_corner_buttons(p)

            # NTP 徽标
            self._draw_badge(p)

            # 亮度：只在「画面压暗」模式下叠加（真背光模式不加黑，
            # 否则等于压暗两次，且背光已经降下来了没必要再牺牲对比度）
            if self._bright_impl == "overlay" and self._bright_now < 100:
                p.fillRect(self.rect(), QColor(0, 0, 0, brightness_mod.overlay_alpha(self._bright_now)))

            # Toast
            if time.monotonic() < self._toast_until:
                self._draw_toast(p)
        except Exception:
            # 绘制异常必须完整落盘——否则只看到 Qt 的 "Error calling Python
            # override of paintEvent" 空壳信息，排查全靠猜
            try:
                import traceback
                with open(os.path.join(os.environ.get("TEMP", "."),
                                       "flipclock-paint-error.log"), "a",
                          encoding="utf-8", errors="replace") as fh:
                    fh.write(traceback.format_exc() + "\n")
            except OSError:
                pass
        finally:
            # PySide6 中 QPainter 销毁时机不受控：必须在这里显式且仅一次结束
            if p.isActive():
                p.end()

    def _draw_card(self, p, rect, card):
        card_h = rect.height()
        r = card_h * 0.06
        path = QPainterPath()
        path.addRoundedRect(rect, r, r)

        # 假外投影（3 层递进，避免 QGraphicsDropShadow 的性能开销）
        for dy, alpha in ((2, 26), (5, 18), (8, 10)):
            sp = QPainterPath()
            sp.addRoundedRect(QRectF(rect.left(), rect.top() + dy, rect.width(), rect.height()), r, r)
            p.fillPath(sp, QColor(0, 0, 0, alpha))

        # 底板：上下半卡明暗渐变
        grad = QLinearGradient(0, rect.top(), 0, rect.bottom())
        grad.setColorAt(0.0, CARD_TOP)
        grad.setColorAt(0.496, CARD_TOP)
        grad.setColorAt(0.504, CARD_BOT)
        grad.setColorAt(1.0, CARD_BOT)
        p.fillPath(path, grad)

        ym = rect.top() + card_h / 2.0

        # 静态数字（上半按动画阶段选 prev/cur，下半恒 cur）
        p.save()
        p.setClipPath(path)

        # 卡片自身的明暗层次先画（只作用于卡底）：
        # 顶部内高光 + 底部内阴影。必须画在数字【之前】——画在之后会把数字
        # 下半压成灰色渐变，数字越大越像渲染缺陷（2026-11-08 真机反馈修正）。
        p.setPen(QColor(255, 255, 255, 15))
        p.drawLine(rect.left() + r, rect.top() + 0.5, rect.right() - r, rect.top() + 0.5)
        sh_h = card_h * 0.12
        lg = QLinearGradient(0, rect.bottom() - sh_h, 0, rect.bottom())
        lg.setColorAt(0.0, QColor(0, 0, 0, 0))
        lg.setColorAt(1.0, QColor(0, 0, 0, 85))
        p.fillRect(QRectF(rect.left() + 2, rect.bottom() - sh_h, rect.width() - 4, sh_h), lg)

        # 数字（保持纯白，不被任何阴影盖住）
        # 上半在翻页前半程显示「旧数字」；若 prev 还不是有效字形（首次启动的占位），
        # 就用当前数字，避免开机瞬间上半卡片空白 350ms（2026-11-08 渲染检查发现）
        top_ch = card.cur
        if 0.0 <= card.anim_t < 0.5 and card.prev in "0123456789":
            top_ch = card.prev
        self._draw_glyph_half(p, rect, top_ch, True)
        self._draw_glyph_half(p, rect, card.cur, False)

        # 翻页轴中线压在数字之上（与设计稿一致）
        p.setPen(QColor(0, 0, 0, 160))
        p.drawLine(rect.left() + r, ym, rect.right() - r, ym)
        p.setPen(QColor(255, 255, 255, 11))
        p.drawLine(rect.left() + r, ym + 1, rect.right() - r, ym + 1)
        p.restore()

        # 翻页动画覆盖层
        if 0.0 <= card.anim_t < 1.0:
            t = card.anim_t
            p.save()
            p.setClipPath(path)
            if t < 0.5:
                # 旧上半翻落：绕中线压缩
                sy = 1.0 - 2.0 * t
                p.translate(0, ym)
                p.scale(1.0, sy)
                p.translate(0, -ym)
                self._draw_glyph_half(p, rect, card.prev, True)
                p.restore()
                # 翻落产生的中线上方动态阴影
                band = min(9.0, t * 22.0)
                sg = QLinearGradient(0, ym - band, 0, ym)
                sg.setColorAt(0.0, QColor(0, 0, 0, 0))
                sg.setColorAt(1.0, QColor(0, 0, 0, int(90 * t * 2)))
                p.fillRect(QRectF(rect.left() + 1, ym - band, rect.width() - 2, band), sg)
            else:
                # 新下半翻入：从中线展开
                sy = 2.0 * t - 1.0
                p.translate(0, ym)
                p.scale(1.0, sy)
                p.translate(0, -ym)
                self._draw_glyph_half(p, rect, card.cur, False)
                p.restore()
                # 展开页顶部阴影随展开淡出
                h = card_h * 0.5 * sy
                sg = QLinearGradient(0, ym, 0, ym + h)
                a = int(120 * (1.0 - (t - 0.5) * 2.0))
                sg.setColorAt(0.0, QColor(0, 0, 0, a))
                sg.setColorAt(1.0, QColor(0, 0, 0, 0))
                p.fillRect(QRectF(rect.left() + 1, ym, rect.width() - 2, h), sg)
                return
            return

    def _draw_glyph_half(self, p, rect, ch, top_half: bool):
        pm = self._glyph(ch, rect.height())
        if pm is None or pm.isNull():
            return
        x = rect.center().x() - pm.width() / 2.0
        y = rect.center().y() - pm.height() / 2.0
        clip = QRectF(rect) if top_half else QRectF(rect)
        clip.setHeight(rect.height() / 2.0)
        if not top_half:
            clip.moveTop(rect.top() + rect.height() / 2.0)
        p.save()
        p.setClipRect(clip.toRect())
        p.drawPixmap(int(x), int(y), pm)
        p.restore()

    def _draw_colon(self, p, rect):
        card_h = rect.height()
        d = card_h * 0.072  # 略放大：数字变大后 0.062 的点显得太弱
        gap = card_h * 0.10
        cx = rect.center().x()
        # 光学居中：整体中心略上移
        cy = rect.center().y() - card_h * 0.02
        if self._seconds_mode() == "off":
            color = COLON_ON   # 无秒模式：常态点亮，与数字同亮
        elif self._colon_bright:
            color = COLON_ON
        else:
            color = COLON_OFF
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QRectF(cx - d / 2, cy - gap / 2 - d / 2, d, d))
        p.drawEllipse(QRectF(cx - d / 2, cy + gap / 2 - d / 2, d, d))

    def _draw_ampm(self, p):
        font = QFont("Segoe UI")
        font.setPixelSize(max(9, int(self._card_h * 0.085)))
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 3)
        p.setFont(font)
        p.setPen(DATE_C)
        any_rect = next(iter(self._rects.values()))
        line_h = self._card_h * 0.13
        # 两行整体垂直居中（略上偏），与设计稿一致
        top = any_rect.center().y() - line_h - self._card_h * 0.03
        x = self._ampm_x
        letters = "AM" if self._last_ap == "AM" else "PM"
        for i, ch in enumerate(letters):
            p.drawText(QRectF(x - 10, top + i * line_h, 30, line_h),
                       Qt.AlignHCenter, ch)

    def _draw_date(self, p):
        px = _date_px(self.width())
        font = QFont("Segoe UI")
        font.setPixelSize(int(px))
        font.setWeight(QFont.Weight.Medium)  # 比常规略粗，远看更清楚
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, self.width() * 0.006)
        p.setFont(font)
        p.setPen(DATE_C)
        p.drawText(self._date_rect, Qt.AlignHCenter | Qt.AlignVCenter, self._last_date)

    # ---------------- 角落按钮 ----------------

    def _btn_rect(self, which) -> QRectF:
        m = 10
        if which == "exit":
            return QRectF(self.width() - BTN_SIZE - m, m, BTN_SIZE, BTN_SIZE)
        return QRectF(m, m, BTN_SIZE, BTN_SIZE)

    def _draw_corner_buttons(self, p):
        for which in ("exit", "gear"):
            rect = self._btn_rect(which)
            hovered = self._hover_btn == which
            pressed = self._pressed_btn == which
            if hovered or pressed:
                rr = QPainterPath()
                rr.addRoundedRect(rect, 10, 10)
                p.fillPath(rr, QColor(255, 255, 255, 36 if pressed else 18))
            alpha = 255 if (hovered or pressed) else 71  # 0.28 常态
            col = QColor(246, 246, 248, alpha)
            c = rect.center()
            k = rect.width() * 0.22
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(col)
            if which == "exit":
                # minimize-2 风格：双对角箭头向内
                pen = p.pen()
                pen.setWidthF(2.2)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawLine(c.x() - k, c.y() + k, c.x() + k * 0.55, c.y() - k * 0.55)
                p.drawLine(c.x() - k * 0.55, c.y() - k, c.x() + k, c.y() + k * 0.55)
                # 端头小箭头
                ah = k * 0.34
                p.drawLine(c.x() + k * 0.55, c.y() - k * 0.55,
                           c.x() + k * 0.55 - ah, c.y() - k * 0.55)
                p.drawLine(c.x() + k * 0.55, c.y() - k * 0.55,
                           c.x() + k * 0.55, c.y() - k * 0.55 + ah)
                p.drawLine(c.x() + k, c.y() + k * 0.55,
                           c.x() + k - ah, c.y() + k * 0.55)
                p.drawLine(c.x() + k, c.y() + k * 0.55,
                           c.x() + k, c.y() + k * 0.55 - ah)
            else:
                # 齿轮：外圈齿 + 中心孔
                pen = p.pen()
                pen.setWidthF(2.0)
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                r_out = k * 0.78
                r_tooth = r_out * 1.32
                for i in range(8):
                    a = i * math.pi / 4.0
                    p.drawLine(c.x() + r_out * math.cos(a), c.y() + r_out * math.sin(a),
                               c.x() + r_tooth * math.cos(a), c.y() + r_tooth * math.sin(a))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(c, r_out, r_out)
                p.drawEllipse(c, r_out * 0.36, r_out * 0.36)

    # ---------------- 徽标 / Toast ----------------

    def _draw_badge(self, p):
        color, text, synced = self._badge_state()
        font = QFont("Segoe UI")
        font.setPixelSize(12)
        p.setFont(font)
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(text)
        x = self.width() - 16 - tw - 14
        y = self.height() - 14 - fm.height()
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawEllipse(QRectF(x, y + fm.height() / 2 - 3.5, 7, 7))
        p.setPen(color if synced else DIM)
        p.drawText(int(x + 14), int(y), tw + 4, fm.height(),
                   Qt.AlignVCenter, text)

    def _draw_toast(self, p):
        text = "双击任意位置即可退出全屏"
        font = QFont("Segoe UI")
        font.setPixelSize(13)
        p.setFont(font)
        fm = QFontMetrics(font)
        tw = fm.horizontalAdvance(text)
        bw = tw + 18 * 2 + 26
        bh = 38
        rect = QRectF((self.width() - bw) / 2, self.height() - 46 - bh, bw, bh)
        path = QPainterPath()
        path.addRoundedRect(rect, 10, 10)
        p.fillPath(path, QColor(22, 22, 28, 235))
        p.setPen(QColor("#2e2e38"))
        p.drawPath(path)
        # tap 图标：实心点 + 两圈波纹
        cx = rect.left() + 20
        cy = rect.center().y()
        p.setPen(QPen(QColor(0x7f, 0x7f, 0x88), 1.4))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(cx - 3, cy - 3, 6, 6))
        p.drawEllipse(QRectF(cx - 7, cy - 7, 14, 14))
        p.setPen(QColor("#cfcfd6"))
        p.drawText(int(rect.left() + 34), int(rect.top()), int(rect.width() - 40),
                   int(rect.height()), Qt.AlignVCenter, text)

    # ---------------- 交互 ----------------

    def _hit_btn(self, pos) -> str | None:
        for which in ("exit", "gear"):
            if self._btn_rect(which).contains(pos):
                return which
        return None

    def mouseMoveEvent(self, event):
        hit = self._hit_btn(event.position().toPoint())
        if hit != self._hover_btn:
            self._hover_btn = hit
            for which in ("exit", "gear"):
                self.update(self._btn_rect(which).toAlignedRect())

    def leaveEvent(self, event):
        if self._hover_btn:
            self._hover_btn = None
            self.update()

    def mousePressEvent(self, event):
        hit = self._hit_btn(event.position().toPoint())
        if hit == "exit":
            self._pressed_btn = hit
            self.update(self._btn_rect(hit).toAlignedRect())
            self.exit_fullscreen_requested.emit()
        elif hit == "gear":
            self._pressed_btn = hit
            self.update(self._btn_rect(hit).toAlignedRect())
            self.panel_toggle_requested.emit()

    def mouseReleaseEvent(self, event):
        if self._pressed_btn:
            self.update(self._btn_rect(self._pressed_btn).toAlignedRect())
            self._pressed_btn = None

    def mouseDoubleClickEvent(self, event):
        if self._hit_btn(event.position().toPoint()):
            return
        self.toggle_fullscreen_requested.emit()

    def keyPressEvent(self, event):
        # 三通道退出全屏之一：Esc（外接键盘/平板键盘盖）
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.exit_fullscreen_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    # ---------------- 系统时间变更 ----------------

    def nativeEvent(self, eventType, message):
        # WM_TIMECHANGE（0x001E）：手动改系统时间/时区时立即刷新，不等下一秒
        if eventType == b"windows_generic_msg":
            try:
                import ctypes
                msg = ctypes.wintypes.MSG.from_address(int(message))
                if msg.message == 0x001E:
                    self.refresh_now()
            except Exception:
                pass
        return super().nativeEvent(eventType, message)
