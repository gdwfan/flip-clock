"""翻页时钟 · 入口。

组装：QApplication / 单实例锁 + 激活已有实例 / 全屏窗口 / 设置面板 / 常亮 / NTP / 日志重定向。

命令行：
  --bg, --background   后台模式：窗口化且**不抢前台焦点、不压到别的窗口上面**（开发期边干活边看效果用）
                       不拦息屏、保持窗口化；点它/Alt-Tab 时仍能正常切到前台

AUTOTEST 钩子（冒烟用，全部环境变量）：
  FLIPCLOCK_AUTOTEST=1        窗口 1280x800 不全屏、不抢常亮、不起 NTP、配置写入临时目录
  FLIPCLOCK_SCREENSHOT=path   启动数秒后截图保存并退出（退出码 0）
  FLIPCLOCK_PANEL=1           启动即打开设置面板
  FLIPCLOCK_FREEZE_ANIM=p     把翻页动画钉在进度 p（验证中间态）
  FLIPCLOCK_CFG=<json>        覆盖配置项
  FLIPCLOCK_FAKE_HOUR=h       伪造当前小时（验证自动亮度的白天/夜间两档）
  FLIPCLOCK_ALLOW_BRIGHT=1    AUTOTEST 下也允许真写系统背光（默认禁止，免得测试动了开发机）
  FLIPCLOCK_DURATION=sec      到时自动退出（内存老化测试用）
"""
import json
import os
import sys
import traceback

from PySide6.QtCore import QEvent, QLockFile, QObject, Qt, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

import settings as settings_mod
import screen_bright
from clock_widget import ClockWidget
from debounce import SaveDebouncer
from ntp_check import NtpChecker
from settings_panel import SettingsPanel

# 自定义命令行开关要在建 QApplication 之前摘掉，别塞给 Qt 去解析
ARG_BG = "--bg"
BG = os.environ.get("FLIPCLOCK_BACKGROUND") == "1" or ARG_BG in sys.argv
if ARG_BG in sys.argv:
    sys.argv = [a for a in sys.argv if a != ARG_BG]

AUTOTEST = os.environ.get("FLIPCLOCK_AUTOTEST") == "1"
SCREENSHOT = os.environ.get("FLIPCLOCK_SCREENSHOT")
PANEL = os.environ.get("FLIPCLOCK_PANEL") == "1"
TOAST = os.environ.get("FLIPCLOCK_TOAST") == "1"
FREEZE = float(os.environ.get("FLIPCLOCK_FREEZE_ANIM", "0") or 0)
DURATION = float(os.environ.get("FLIPCLOCK_DURATION", "0") or 0)

APP_TITLE = "翻页时钟"
SERVER_NAME = "FlipClock"


def _start_window(win, bg: bool):
    """起窗口。

    bg=True（后台模式）：窗口化 + 不抢焦点 + 落到其他窗口后面 —— 开发机上一边干别的活
    一边看效果时用。要点三条，缺一条就会"弹到最前面"：
      1. `WA_ShowWithoutActivating`：显示但不激活（不然 Windows 会把键盘焦点抢过去）；
      2. `show()` 而不是 `showFullScreen()`（全屏窗口在 Windows 上必然成为前台窗口）；
      3. `lower()`：新窗口默认在 z 序最上层，不压下去就会盖住用户正在看的窗口。
    bg=False：正常全屏（平板上的真实用法）。
    """
    if bg:
        win.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        win.prepare_windowed()   # 70% 尺寸 + 居中
        win.show()
        win.lower()              # 别盖住用户正在干活的窗口
        return
    win.showFullScreen()


def _asset_path(name: str) -> str:
    """资源路径：打包期走 sys._MEIPASS，开发期走项目根 assets/。

    两种形态必须分开处理（2026-11-08 复查踩到）：
    - 打包 onedir：模块位于 _internal/，--add-data 目标写 assets 也落在 _internal/assets，
      正确基址是 sys._MEIPASS（即 _internal/），而不是 dirname(__file__)/../assets
      （那会指向并不存在的 dist/FlipClock/assets，图标静默丢失）
    - 开发期：本文件在 src/ 下，资源在项目根 assets/，即 dirname(dirname(__file__))/assets
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return os.path.join(meipass, "assets", name)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "assets", name)


def _redirect_stdio():
    """windowed exe 的 stdout/stderr 是 None（GUI 子系统下的常见坑，见脚本头部说明）；统一落日志。"""
    log_dir = settings_mod.CONF_DIR / "logs"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / "error.log"
        if path.exists() and path.stat().st_size > 1_000_000:
            path.replace(path.with_suffix(".log.1"))
        f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        if sys.stdout is None:
            sys.stdout = f
        if sys.stderr is None:
            sys.stderr = f
    except OSError:
        pass

    def _hook(t, v, tb):
        try:
            with open(log_dir / "error.log", "a", encoding="utf-8", errors="replace") as fh:
                fh.write("".join(traceback.format_exception(t, v, tb)) + "\n")
        except OSError:
            pass

    sys.excepthook = _hook


def _set_autostart(enable: bool) -> bool:
    """HKCU\\...\\Run（无需管理员）。开发期写 python 命令行，打包后写 exe 路径。"""
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Run",
                            0, winreg.KEY_SET_VALUE) as key:
            if enable:
                if getattr(sys, "frozen", False):
                    cmd = f'"{sys.executable}"'
                else:
                    cmd = f'"{sys.executable}" "{os.path.abspath(__file__)}"'
                winreg.SetValueEx(key, "FlipClock", 0, winreg.REG_SZ, cmd)
            else:
                try:
                    winreg.DeleteValue(key, "FlipClock")
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


class _PanelRefit(QObject):
    """窗口尺寸变化时保持设置面板几何。"""

    def __init__(self, panel):
        super().__init__()
        self._panel = panel

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.Resize and self._panel.isVisible():
            self._panel.refit()
        return False


def main():
    _redirect_stdio()

    cfg = settings_mod.load()
    if AUTOTEST:
        try:
            cfg.update(json.loads(os.environ.get("FLIPCLOCK_CFG", "{}")))
        except (ValueError, TypeError):
            pass

    app = QApplication(sys.argv)
    app.setApplicationName("FlipClock")
    app.setApplicationDisplayName(APP_TITLE)
    app.setQuitOnLastWindowClosed(True)

    # ---- 单实例：抢锁失败 → 激活已有实例后退出 ----
    lock = QLockFile(os.path.join(os.environ.get("TEMP", "."), "flipclock-instance.lock"))
    if not lock.tryLock(100):
        sock = QLocalSocket()
        sock.connectToServer(SERVER_NAME)
        if sock.waitForConnected(400):
            sock.write(b"raise")
            sock.flush()
            sock.waitForBytesWritten(400)
        return 0
    QLocalServer.removeServer(SERVER_NAME)
    server = QLocalServer()
    server.listen(SERVER_NAME)

    # ---- 常亮 / 校时 ----
    keep_awake = None
    if not AUTOTEST and not BG:
        # 后台模式（开发期看效果）不拦息屏：用户正在这台机器上干别的活
        import keep_awake as ka_mod
        keep_awake = ka_mod
        if cfg.get("keep_awake", True):
            keep_awake.enable()

    ntp = NtpChecker()
    ntp_provider = ntp.snapshot
    if cfg.get("ntp_check", True) and not AUTOTEST:
        ntp.start(delay_s=30)

    # ---- 窗口与面板 ----
    win = ClockWidget(cfg, ntp_provider)
    win.setWindowTitle(APP_TITLE)
    ico = _asset_path("icon.ico")
    if os.path.exists(ico):
        win.setWindowIcon(QIcon(ico))

    panel = SettingsPanel(cfg, ntp_provider, parent=win)
    refit = _PanelRefit(panel)
    win.installEventFilter(refit)

    def _toggle_fullscreen():
        if win.isFullScreen():
            win.showNormal()
            win.prepare_windowed()
        else:
            win.showFullScreen()

    def _exit_fullscreen():
        if win.isFullScreen():
            win.showNormal()
            win.prepare_windowed()

    def _toggle_panel():
        if panel.isVisible():
            panel.hide()
        else:
            panel.open_panel()

    # ---- 配置落盘防抖 ----
    # 拖亮度滑杆会连续触发几十次配置变更，逐次原子写（tmp→rename→.bak）是明显写放大，
    # 也违背「运行期零磁盘写入」的设计承诺（2026-11-08 复查发现）
    saver = SaveDebouncer(lambda: cfg, interval_ms=400)

    def _apply_config(new_cfg):
        nonlocal ntp
        # 必须先复制：下面 cfg.clear() 清空的是同一个 dict 对象，
        # 直接 old = cfg 会让后续所有「旧值 vs 新值」比较全部失效
        # （keep_awake / ntp_check / autostart 的变化检测会漏触发）
        old = dict(cfg)
        win.set_config(new_cfg)
        cfg.clear()
        cfg.update(new_cfg)
        if not AUTOTEST:
            if new_cfg.get("keep_awake", True) != old.get("keep_awake", True):
                if new_cfg["keep_awake"]:
                    keep_awake.enable()
                else:
                    keep_awake.disable()
            if new_cfg.get("ntp_check", True) != old.get("ntp_check", True):
                if new_cfg["ntp_check"]:
                    ntp.start(delay_s=3)
                else:
                    ntp.stop()
            if new_cfg.get("autostart", False) != old.get("autostart", False):
                _set_autostart(new_cfg["autostart"])
        saver.touch()  # 防抖落盘：连续变更只在静默后写一次

    win.exit_fullscreen_requested.connect(_exit_fullscreen)
    win.toggle_fullscreen_requested.connect(_toggle_fullscreen)
    win.panel_toggle_requested.connect(_toggle_panel)
    panel.config_changed.connect(_apply_config)
    panel.quit_requested.connect(app.quit)

    # 已有实例发来激活请求
    def _on_conn():
        while server.hasPendingConnections():
            sock = server.nextPendingConnection()
            if sock is not None:
                sock.deleteLater()  # 不 deleteLater 会每次激活泄漏一个 QLocalSocket
        if win.isMinimized():
            win.showNormal()
        win.raise_()
        win.activateWindow()

    server.newConnection.connect(_on_conn)

    # ---- 显示 ----
    if AUTOTEST:
        win.resize(1280, 800)
        win.showNormal()
        if FREEZE > 0:
            win.freeze_anim_at(FREEZE)
        if PANEL:
            panel.open_panel()
        if TOAST:
            win.show_toast(6.0)
        if SCREENSHOT:
            delay = 2200 if FREEZE else (3000 if TOAST else 5200)
            QTimer.singleShot(delay, lambda: _capture())
        if DURATION > 0:
            QTimer.singleShot(int(DURATION * 1000), app.quit)
    else:
        _start_window(win, BG)
        if not cfg.get("first_run_done", False):
            win.show_toast(4.2)
            cfg["first_run_done"] = True
            settings_mod.save(dict(cfg))
        if BG:
            print("后台模式：窗口化、不抢焦点、不拦息屏（--bg）", flush=True)

    def _cleanup():
        if saver.pending():
            saver.flush()  # 退出前把待写配置落盘，别丢用户刚改的设置
        # 真背光模式下我们改的是系统电源计划的亮度值 → 退出必须还原，
        # 否则用户拿平板干别的时屏幕还停在「夜间档 40%」（2026-11-10）
        screen_bright.restore()
        ntp.stop()
        if keep_awake is not None:
            keep_awake.disable()
        server.close()
        lock.unlock()

    app.aboutToQuit.connect(_cleanup)

    def _capture():
        ok = win.grab().save(SCREENSHOT)
        print(f"AUTOTEST screenshot={'ok' if ok else 'FAIL'} -> {SCREENSHOT}", flush=True)
        app.exit(0 if ok else 3)

    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
