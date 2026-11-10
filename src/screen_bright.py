"""屏幕背光控制：Power API（powrprof.dll），与 Windows 设置里那根亮度条同源。

为什么要单独一个模块（2026-11-10 用户提问后加）：
- 原来的「全窗黑色叠加」只是**像素**变暗，背光仍满功率跑 → 夜里照样刺眼、也不省电。
  低配平板 7×24 常亮，用户要的「白天亮/晚上暗」应该是真背光。
- 纯 ctypes 直调 powrprof.dll，零新增依赖（不引 pywin32，打包体积不变）。
- 设备没有 ACPI 亮度接口时 available() 直接 False，调用方自动退回画面压暗。

已知副作用（接受，且与用户手拖 Windows 亮度条效果一致）：写的是**当前活动电源计划**的
亮度值，即会改掉该计划里的设置。所以退出时 main 必须调 restore() 还原。
AUTOTEST 下默认完全不碰系统亮度（否则跑一次冒烟就把开发机调暗了），
只有显式 FLIPCLOCK_ALLOW_BRIGHT=1 才真写。
"""
import ctypes
import os
import uuid
from ctypes import POINTER, byref, c_ubyte, c_void_p
from ctypes import wintypes

_SUBGROUP_VIDEO = "{7516b95f-f776-4464-8c53-06167f40cc99}"
_SETTING_BRIGHTNESS = "{aded5e82-b909-4619-9949-f5d71dac0bcb}"
LEVEL_MIN, LEVEL_MAX = 0, 100


class _GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", c_ubyte * 8)]

    @classmethod
    def of(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class ScreenBrightness:
    """一把句柄包住全部 ctypes 细节；任何一步失败都记为「不支持」，绝不抛异常给调用方。"""

    def __init__(self):
        self.ok = False          # 探测结果：本机能不能调背光
        self.original = None     # 首次读到的原值（退出时还原用）
        self.wrote = False       # 本进程是否真写过 → 决定要不要还原
        self._powr = None
        self._kern = None
        self._loaded = False

    # ---- 初始化 ----

    def _load(self):
        if self._loaded:
            return self.ok
        self._loaded = True
        try:
            self._powr = ctypes.WinDLL("powrprof.dll")
            self._kern = ctypes.WinDLL("kernel32.dll")
            p, h = self._powr, self._kern
            p.PowerGetActiveScheme.argtypes = [wintypes.HANDLE, POINTER(POINTER(_GUID))]
            p.PowerReadACValueIndex.argtypes = [wintypes.HANDLE, POINTER(_GUID),
                                                POINTER(_GUID), POINTER(_GUID),
                                                POINTER(wintypes.DWORD)]
            p.PowerReadDCValueIndex.argtypes = p.PowerReadACValueIndex.argtypes
            p.PowerWriteACValueIndex.argtypes = [wintypes.HANDLE, POINTER(_GUID),
                                                 POINTER(_GUID), POINTER(_GUID),
                                                 wintypes.DWORD]
            p.PowerWriteDCValueIndex.argtypes = p.PowerWriteACValueIndex.argtypes
            p.PowerSetActiveScheme.argtypes = [wintypes.HANDLE, POINTER(_GUID)]
            h.LocalFree.argtypes = [c_void_p]
        except (OSError, AttributeError):
            self.ok = False
            return False
        self.ok = self.get() is not None
        return self.ok

    # ---- 对内：拿活动电源计划 ----

    def _scheme(self):
        scheme = POINTER(_GUID)()
        if self._powr.PowerGetActiveScheme(None, byref(scheme)) != 0 or not scheme:
            return None
        return scheme

    def _free(self, scheme):
        try:
            self._kern.LocalFree(ctypes.cast(scheme, c_void_p))
        except Exception:  # noqa: BLE001  释放失败不该影响功能
            pass

    # ---- 对外 ----

    def allow(self) -> bool:
        """AUTOTEST 默认不许碰系统亮度（跑冒烟不能顺手动开发机）。"""
        if os.environ.get("FLIPCLOCK_AUTOTEST") == "1":
            return os.environ.get("FLIPCLOCK_ALLOW_BRIGHT") == "1"
        return True

    def get(self):
        """当前亮度百分比；不支持/失败返回 None。"""
        if not self._loaded:
            return None
        scheme = self._scheme()
        if scheme is None:
            return None
        try:
            ac, dc = wintypes.DWORD(0), wintypes.DWORD(0)
            r1 = self._powr.PowerReadACValueIndex(None, scheme, byref(_GUID.of(_SUBGROUP_VIDEO)),
                                                  byref(_GUID.of(_SETTING_BRIGHTNESS)), byref(ac))
            r2 = self._powr.PowerReadDCValueIndex(None, scheme, byref(_GUID.of(_SUBGROUP_VIDEO)),
                                                  byref(_GUID.of(_SETTING_BRIGHTNESS)), byref(dc))
            if r1 != 0 or r2 != 0:
                return None
            level = int(ac.value)
            if not LEVEL_MIN <= level <= LEVEL_MAX:
                return None
            if self.original is None:
                self.original = level
            return level
        finally:
            self._free(scheme)

    def set(self, level: int) -> bool:
        """写背光（AC/DC 都写，然后激活当前计划使其立刻生效）。"""
        if not self._load() or not self.allow():
            return False
        level = max(LEVEL_MIN, min(LEVEL_MAX, int(level)))
        scheme = self._scheme()
        if scheme is None:
            return False
        try:
            sub = _GUID.of(_SUBGROUP_VIDEO)
            setg = _GUID.of(_SETTING_BRIGHTNESS)
            r1 = self._powr.PowerWriteACValueIndex(None, scheme, byref(sub), byref(setg), level)
            r2 = self._powr.PowerWriteDCValueIndex(None, scheme, byref(sub), byref(setg), level)
            r3 = self._powr.PowerSetActiveScheme(None, scheme)
            if r1 == 0 and r2 == 0 and r3 == 0:
                self.wrote = True   # 记下"我动过"，退出/切走时才需要还原
                return True
            return False
        finally:
            self._free(scheme)

    def restore(self) -> bool:
        """还原首次读到的原值。**只有本进程真写过才动手**——否则画面压暗模式下退出时
        会把用户自己刚在 Windows 里调好的亮度踩回去（2026-11-10 加切换功能时收紧）。"""
        if not self.ok or self.original is None or not self.wrote:
            return False
        if not self.set(self.original):
            return False
        self.wrote = False
        return True

    def available(self) -> bool:
        return self._load()


_default = ScreenBrightness()


def available() -> bool:
    return _default.available() and _default.allow()


def get():
    return _default.get() if _default.available() else None


def set(level: int) -> bool:  # noqa: A001  与模块语义一致，故意遮蔽内置名
    return _default.set(level)


def restore() -> bool:
    return _default.restore()
