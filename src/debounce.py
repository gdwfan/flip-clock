"""配置落盘防抖：连续变更只在静默 interval_ms 之后写一次。

为什么需要：拖一次亮度滑杆会产生几十次配置变更，逐次原子写（tmp→rename→.bak）
是明显的写放大，也违背「运行期零磁盘写入」的设计承诺。
（2026-11-08 复查发现；抽成独立组件是为了让回归测试能直接测产品代码）
"""
from PySide6.QtCore import QObject, QTimer


def _default_saver(cfg):
    import settings as settings_mod
    settings_mod.save(cfg)


class SaveDebouncer(QObject):
    """touch() 重置计时；静默 interval_ms 后自动 flush；退出前可显式 flush。"""

    def __init__(self, snapshot, interval_ms=400, saver=None, parent=None):
        super().__init__(parent)
        self._snapshot = snapshot  # () -> dict：取当前配置快照
        self._saver = saver or _default_saver
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self.flush)
        self.save_count = 0  # 累计落盘次数（测试断言 / 排障可用）

    def touch(self):
        """配置发生变更后调用：重置防抖计时。"""
        self._timer.start()

    def flush(self):
        """立即落盘（防抖到期，或退出前保证不丢配置）。"""
        if self._timer.isActive():
            self._timer.stop()
        self._saver(dict(self._snapshot()))
        self.save_count += 1

    def pending(self) -> bool:
        return self._timer.isActive()
