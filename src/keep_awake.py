"""屏幕常亮：SetThreadExecutionState。运行期阻止息屏，退出/关闭开关即恢复系统默认。

只在主线程调用（该 API 语义是线程级执行状态）。
"""
import ctypes

ES_CONTINUOUS = 0x80000000
ES_DISPLAY_REQUIRED = 0x00000002

_active = False


def enable():
    global _active
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED)
    _active = True


def disable():
    global _active
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
    _active = False


def is_active() -> bool:
    return _active
