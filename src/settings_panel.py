"""设置面板：左上齿轮唤出的浮层（设计稿 §设置面板）。

7+1 项配置即时生效并写盘；开关为自绘 iOS 风格控件（QSS 画不出滑块圆点）；
daemon 线程产出的 NTP 状态由 UI 定时器拉取渲染，严格遵守线程铁律。
"""
import time

from PySide6.QtCore import Qt, QTimer, Signal, QVariantAnimation
from PySide6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import brightness as brightness_mod
import version as version_mod
import ntp_check
import screen_bright

PANEL_W = 340


PANEL_QSS = """
QWidget#settingsPanel {
    background: #141419;
    border: 1px solid #2a2a32;
    border-radius: 14px;
}
QLabel { color: #d6d6dc; font-size: 13.5px; background: transparent; }
QLabel#panelTitle { color: #f6f6f8; font-size: 14px; font-weight: 600; }
QLabel.sub { color: #7f7f88; font-size: 11.5px; }
QLabel.ver { color: #55555e; font-size: 11.5px; }
QLabel.ntpStatus { color: #7f7f88; font-size: 12px; }
QToolButton {
    background: transparent; color: #7f7f88;
    font-size: 12.5px; padding: 6px 9px; border-radius: 7px;
}
QToolButton:checked {
    background: #e8e8ec; color: #17171c; font-weight: 600;
}
QFrame#segBox {
    background: #0d0d12; border: 1px solid #2a2a32; border-radius: 9px;
}
QFrame#rowLine { background: #22222a; max-height: 1px; border: none; }
QPushButton#quitBtn {
    background: transparent; color: #c9c9d0;
    border: 1px solid #3a3a44; border-radius: 8px;
    font-size: 12.5px; padding: 7px 14px;
}
QPushButton#quitBtn:hover { background: #26262f; border-color: #4a4a56; }
QSlider::groove:horizontal {
    height: 4px; border-radius: 2px; background: #2b2b34;
}
QSlider::sub-page:horizontal { background: #d9d9df; border-radius: 2px; }
QSlider::handle:horizontal {
    width: 17px; height: 17px; margin: -7px 0;
    border-radius: 8px; background: #efeff2; border: none;
}
QLabel.pct { color: #7f7f88; font-size: 12px; }
"""


class Switch(QWidget):
    """iOS 风格开关（自绘：QSS 无法表达滑块圆点动画）。"""

    toggled = Signal(bool)

    def __init__(self, checked=False, parent=None):
        super().__init__(parent)
        self.setFixedSize(44, 25)
        self._pos = 1.0 if checked else 0.0  # 0=off 1=on
        self._checked = checked
        self._anim = QVariantAnimation(self)
        self._anim.setDuration(150)
        self._anim.valueChanged.connect(self._on_v)

    def _on_v(self, v):
        self._pos = float(v)
        self.update()

    def setChecked(self, checked, emit=False):
        if checked == self._checked:
            return
        self._checked = checked
        self._animate()
        if emit:
            self.toggled.emit(checked)

    def isChecked(self):
        return self._checked

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._checked = not self._checked
            self._animate()
            self.toggled.emit(self._checked)

    def _animate(self):
        if self._anim.state() == QVariantAnimation.Running:
            self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if self._checked else 0.0)
        self._anim.start()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # 轨道
        track = QColor("#d9d9df") if self._pos > 0.5 else QColor("#2b2b34")
        # 过渡期颜色插值
        if 0.0 < self._pos < 1.0:
            a, b = QColor("#2b2b34"), QColor("#d9d9df")
            t = self._pos
            track = QColor(int(a.red() + (b.red() - a.red()) * t),
                           int(a.green() + (b.green() - a.green()) * t),
                           int(a.blue() + (b.blue() - a.blue()) * t))
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(0, 0, 44, 25, 12.5, 12.5)
        # 圆点
        knob_on = QColor("#17171c")
        knob_off = QColor("#9a9aa4")
        knob = knob_off if self._pos < 0.5 else knob_on
        if 0.0 < self._pos < 1.0:
            t = self._pos
            a, b = knob_off, knob_on
            knob = QColor(int(a.red() + (b.red() - a.red()) * t),
                          int(a.green() + (b.green() - a.green()) * t),
                          int(a.blue() + (b.blue() - a.blue()) * t))
        x = 3 + (44 - 6 - 19) * self._pos
        p.setBrush(knob)
        p.drawEllipse(int(x), 3, 19, 19)
        p.end()


def _row(label, sub=None, right_widget=None):
    """一行：左侧 label(+sub)，右侧控件。行高 46（触摸目标）。

    标题固定 objectName=rowTitle，供调用方事后改文案（亮度行的标题随自动开关变）。
    """
    holder = QWidget()
    holder.setObjectName("rowHolder")
    lay = QHBoxLayout(holder)
    lay.setContentsMargins(18, 4, 18, 4)
    lay.setSpacing(14)
    txt = QVBoxLayout()
    txt.setSpacing(2)
    lab = QLabel(label)
    lab.setObjectName("rowTitle")
    txt.addWidget(lab)
    if sub:
        s = QLabel(sub)
        s.setProperty("class", "sub")
        s.setObjectName("sub")
        txt.addWidget(s)
    lay.addLayout(txt)
    lay.addStretch(1)
    if right_widget is not None:
        lay.addWidget(right_widget)
    holder.setMinimumHeight(46)
    return holder


class SettingsPanel(QWidget):
    config_changed = Signal(dict)  # 单项变更后的完整配置
    quit_requested = Signal()

    def __init__(self, cfg: dict, ntp_status_provider, parent=None):
        super().__init__(parent)
        self.setObjectName("settingsPanel")
        # QWidget 子类用 QSS 画背景的前提：不设这个属性 QSS 的 background 不生效，
        # 面板会变透明把底下的时钟透出来（QWidget 子类用 QSS 背景的经典坑）
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(PANEL_QSS)
        self._cfg = dict(cfg)
        self._ntp = ntp_status_provider

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 6, 0, 8)
        lay.setSpacing(0)

        # 标题行
        head = QWidget()
        hl = QHBoxLayout(head)
        hl.setContentsMargins(18, 10, 14, 8)
        title = QLabel("设置")
        title.setObjectName("panelTitle")
        close = QToolButton()
        close.setText("✕")
        close.clicked.connect(self.hide)
        hl.addWidget(title)
        hl.addStretch(1)
        hl.addWidget(close)
        lay.addWidget(head)

        # 时间制式（分段）
        seg_box = QFrame()
        seg_box.setObjectName("segBox")
        sl = QHBoxLayout(seg_box)
        sl.setContentsMargins(2, 2, 2, 2)
        sl.setSpacing(2)
        self._b24 = QToolButton()
        self._b24.setText("24 小时")
        self._b24.setCheckable(True)
        self._b12 = QToolButton()
        self._b12.setText("12 小时")
        self._b12.setCheckable(True)
        grp = QButtonGroup(self)
        grp.setExclusive(True)
        grp.addButton(self._b24)
        grp.addButton(self._b12)
        self._b24.clicked.connect(lambda: self._set("fmt24", True))
        self._b12.clicked.connect(lambda: self._set("fmt24", False))
        (self._b24 if cfg.get("fmt24", True) else self._b12).setChecked(True)
        sl.addWidget(self._b24)
        sl.addWidget(self._b12)
        lay.addWidget(_row("时间制式", None, seg_box))

        # 秒显示三档（同大 / 小号 / 关闭）——小号与关闭会让时:分自动变大
        sec_box = QFrame()
        sec_box.setObjectName("segBox")
        sl = QHBoxLayout(sec_box)
        sl.setContentsMargins(2, 2, 2, 2)
        sl.setSpacing(2)
        sec_grp = QButtonGroup(self)
        sec_grp.setExclusive(True)
        self._sec_btns = {}
        for key, label in (("big", "同大"), ("small", "小号"), ("off", "关闭")):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self._set("seconds_mode", k))
            sec_grp.addButton(b)
            sl.addWidget(b)
            self._sec_btns[key] = b
        cur_mode = cfg.get("seconds_mode")
        if cur_mode not in ("big", "small", "off"):
            cur_mode = "big" if cfg.get("show_seconds", True) else "off"
        self._sec_btns[cur_mode].setChecked(True)
        lay.addWidget(_row("秒显示", "关秒后时:分更大", sec_box))

        self._sw_date = Switch(cfg.get("show_date", True))
        self._sw_date.toggled.connect(lambda v: self._set("show_date", v))
        lay.addWidget(_row("显示日期", None, self._sw_date))

        self._sw_flip = Switch(cfg.get("flip_anim", True))
        self._sw_flip.toggled.connect(lambda v: self._set("flip_anim", v))
        lay.addWidget(_row("翻页动画", "关闭后数字直接跳变，最省电", self._sw_flip))

        # 自动亮度：按时间在白天/夜间两档之间过渡
        self._sw_auto = Switch(cfg.get("auto_bright", True))
        self._sw_auto.toggled.connect(lambda v: self._set("auto_bright", v))
        self._auto_row = _row("自动亮度", "白天亮、夜间暗，按时间平滑过渡", self._sw_auto)
        lay.addWidget(self._auto_row)

        # 亮度方式：背光 = 真调屏幕硬件亮度（省电、夜里不刺眼）；压暗 = 画面叠黑（不动系统设置）
        impl_box = QFrame()
        impl_box.setObjectName("segBox")
        il = QHBoxLayout(impl_box)
        il.setContentsMargins(2, 2, 2, 2)
        il.setSpacing(2)
        self._impl_btns = {}
        impl_grp = QButtonGroup(self)
        impl_grp.setExclusive(True)
        for key, label in (("backlight", "背光"), ("overlay", "压暗")):
            b = QToolButton()
            b.setText(label)
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, k=key: self._set("bright_impl", k))
            impl_grp.addButton(b)
            il.addWidget(b)
            self._impl_btns[key] = b
        self._impl_row = _row("亮度方式", "背光=调硬件亮度；压暗=叠黑", impl_box)
        lay.addWidget(self._impl_row)
        self._sync_impl_row()

        # 白天档（仅自动模式显示）
        self._day_row, self._day_slider, self._day_pct = self._bright_row(
            "白天亮度", "100% 即屏幕满亮", "bright_day")
        lay.addWidget(self._day_row)

        # 夜间档（自动模式）/ 固定亮度（手动模式）
        self._night_row, self._slider, self._pct = self._bright_row(
            "夜间亮度", "卧室建议 40% 以下", "brightness")
        lay.addWidget(self._night_row)
        self._sync_bright_rows()

        self._sw_keep = Switch(cfg.get("keep_awake", True))
        self._sw_keep.toggled.connect(lambda v: self._set("keep_awake", v))
        lay.addWidget(_row("屏幕常亮", "运行时阻止系统自动息屏", self._sw_keep))

        self._sw_ntp = Switch(cfg.get("ntp_check", True))
        self._sw_ntp.toggled.connect(lambda v: self._set("ntp_check", v))
        lay.addWidget(_row("网络校时", "每日一次，静默失败不影响走时", self._sw_ntp))

        self._sw_autostart = Switch(cfg.get("autostart", False))
        self._sw_autostart.toggled.connect(lambda v: self._set("autostart", v))
        lay.addWidget(_row("开机自启", "随 Windows 启动", self._sw_autostart))

        # NTP 状态行
        self._ntp_label = QLabel()
        self._ntp_label.setObjectName("ntpStatus")
        self._ntp_label.setContentsMargins(18, 8, 18, 4)
        self._ntp_label.setWordWrap(True)
        lay.addWidget(self._ntp_label)

        # 底部
        line = QFrame()
        line.setObjectName("rowLine")
        lay.addWidget(line)
        foot = QWidget()
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(18, 10, 18, 4)
        quit_btn = QPushButton("⏻  退出应用")
        quit_btn.setObjectName("quitBtn")
        quit_btn.clicked.connect(self.quit_requested.emit)
        ver = QLabel("v" + version_mod.__version__)
        ver.setObjectName("ver")
        fl.addWidget(quit_btn)
        fl.addStretch(1)
        fl.addWidget(ver)
        lay.addWidget(foot)

        # NTP 状态轮询（1s，打开面板时才启动）
        self._poll = QTimer(self)
        self._poll.timeout.connect(self._refresh_ntp_label)
        self._poll.timeout.connect(self._sync_auto_status)

        self.adjustSize()
        # 初始隐藏必须放在最后：早于此调用会先触发 hideEvent（_poll 尚未创建）
        self.setVisible(False)

    # ---- 生效与写盘 ----
    def _bright_row(self, label, sub, key):
        """亮度行：滑杆 + 百分比；拖拽即时生效（落盘走 main 里的防抖）。"""
        holder = QWidget()
        bl = QHBoxLayout(holder)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(8)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(30, 100)
        slider.setValue(int(self._cfg.get(key, 100)))
        # 固定宽度：滑杆默认 sizeHint 会随内容挤压变化，把面板内容宽度顶出 340
        # → 行内文字被静默裁切（2026-11-10 _probe/panel_fit.py 量到）
        slider.setFixedWidth(92)
        pct = QLabel(f"{slider.value()}%")
        pct.setObjectName("pct")
        slider.valueChanged.connect(lambda v, k=key: self._on_bright(k, v))
        bl.addWidget(slider)
        bl.addWidget(pct)
        return _row(label, sub, holder), slider, pct

    def _sync_bright_rows(self):
        """自动模式显示「白天/夜间」两档；手动模式只留一个「屏幕亮度」。"""
        auto = self._sw_auto.isChecked()
        self._day_row.setVisible(auto)
        title = self._night_row.findChild(QLabel, "rowTitle")
        if title is not None:
            title.setText("夜间亮度" if auto else "屏幕亮度")
        sub = self._night_row.findChild(QLabel, "sub")
        if sub is not None:
            sub.setText("卧室建议 40% 以下" if auto else "调暗屏幕，适合卧室")
        self.adjustSize()

    def _sync_impl_row(self):
        """同步「亮度方式」行：设备不支持背光时禁掉该项，并说明实际用的是哪种。"""
        ok = screen_bright.available()
        btn = self._impl_btns["backlight"]
        if btn.isEnabled() != ok:
            btn.setEnabled(ok)
            btn.setToolTip("" if ok else "本机不支持调背光（无 ACPI 亮度接口），只能用画面叠黑")
        cur = brightness_mod.resolve_impl(self._cfg, ok)
        self._impl_btns[cur].setChecked(True)
        sub = self._impl_row.findChild(QLabel, "sub")
        if sub is not None:
            txt = ("背光=调硬件亮度；压暗=叠黑" if ok
                   else "本机不支持背光，只能用叠黑")
            if sub.text() != txt:
                sub.setText(txt)

    def _sync_auto_status(self):
        """自动亮度行的副标题：当前档位（1s 轮询里刷新，内容不变不 setText）。"""
        sub = self._auto_row.findChild(QLabel, "sub")
        if sub is None:
            return
        lv = brightness_mod.current_level(self._cfg)
        if self._sw_auto.isChecked():
            txt = (f"自动 · 当前 {lv}%（白 {self._cfg.get('bright_day', 100)}"
                   f"/夜 {self._cfg.get('brightness', 100)}）")
        else:
            txt = "关闭后按下方滑杆固定亮度"
        if sub.text() != txt:
            sub.setText(txt)

    def _set(self, key, value):
        self._cfg[key] = value
        if key == "auto_bright":
            self._sync_bright_rows()
            self._sync_auto_status()
        elif key == "bright_impl":
            self._sync_impl_row()
        self.config_changed.emit(dict(self._cfg))

    def _on_bright(self, key, v):
        if key == "bright_day":
            self._day_pct.setText(f"{v}%")
        else:
            self._pct.setText(f"{v}%")
        self._cfg[key] = int(v)
        self._sync_auto_status()
        self.config_changed.emit(dict(self._cfg))

    def current_config(self):
        return dict(self._cfg)

    # ---- 面板显隐 ----
    def refit(self):
        """按当前父窗口宽度重算几何（旋转/缩放后调用）。"""
        parent = self.parentWidget()
        if parent:
            w = min(PANEL_W, parent.width() - 32)
            self.setFixedWidth(int(w))
            self.move(16, 62)

    def open_panel(self):
        self.refit()
        self.raise_()
        self.setVisible(True)
        self._refresh_ntp_label()
        self._sync_bright_rows()
        self._sync_impl_row()
        self._sync_auto_status()
        self._poll.start(1000)

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, "_poll"):
            self._poll.stop()

    def _refresh_ntp_label(self):
        snap = self._ntp() if self._ntp else {"status": "init", "offset_s": None}
        if not self._cfg.get("ntp_check", True):
            self._ntp_label.setText("网络校时已关闭，走时使用系统本地时钟")
            return
        if snap["status"] == "ok" and snap["offset_s"] is not None:
            off = snap["offset_s"]
            when = snap["last_check"].strftime("%H:%M") if snap.get("last_check") else "--:--"
            if abs(off) >= ntp_check.WARN_OFFSET_S:
                self._ntp_label.setText(
                    f"上次校验 {when}（{snap.get('server', '')}）· 系统时间偏差 {off:+.1f}s，"
                    f"建议校准 Windows 时间")
            else:
                self._ntp_label.setText(
                    f"今日 {when} 已同步（{snap.get('server', '')}）· 偏差 {off:+.1f}s")
        elif snap["status"] == "fail":
            self._ntp_label.setText("今日校验未完成（网络不可达），走时使用系统本地时钟")
        else:
            self._ntp_label.setText("等待首次校验… 走时使用系统本地时钟")
