# 屏幕背光能力探测：这台机器能不能真调背光（=Windows 设置里那根亮度条同源）。
#
# 三条路线，全部只读、不改任何设置：
#   1. Power API（powrprof.dll，Windows 亮度滑杆写的就是这个）—— 无依赖，运行时 0 成本
#   2. WMI root\wmi:WmiMonitorBrightness —— 需要 win32com(pywin32)，只作对照
#   3. dxva2 SetMonitorBrightness 句柄可用性（DDC/CI）—— 外接显示器路线
#
# 跑法：py -3.10 -X utf8 -I _probe/system_bright_probe.py
import ctypes
import uuid
from ctypes import POINTER, byref, c_ubyte, c_void_p
from ctypes import wintypes

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name} {detail}")


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", c_ubyte * 8)]

    @classmethod
    def of(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


SUBGROUP_VIDEO = GUID.of("{7516b95f-f776-4464-8c53-06167f40cc99}")
SETTING_BRIGHTNESS = GUID.of("{aded5e82-b909-4619-9949-f5d71dac0bcb}")
NO_ACTIVE_SCHEME = GUID.of("{00000000-0000-0000-0000-000000000000}")

powr = ctypes.WinDLL("powrprof.dll")
powr.PowerGetActiveScheme.argtypes = [wintypes.HANDLE, POINTER(POINTER(GUID))]
powr.PowerGetActiveScheme.restype = wintypes.DWORD
powr.PowerReadACValueIndex.argtypes = [wintypes.HANDLE, POINTER(GUID), POINTER(GUID),
                                      POINTER(GUID), POINTER(wintypes.DWORD)]
powr.PowerReadACValueIndex.restype = wintypes.DWORD
powr.PowerReadDCValueIndex.argtypes = powr.PowerReadACValueIndex.argtypes
powr.PowerReadDCValueIndex.restype = wintypes.DWORD
powr.PowerWriteACValueIndex.argtypes = [wintypes.HANDLE, POINTER(GUID), POINTER(GUID),
                                        POINTER(GUID), wintypes.DWORD]
powr.PowerWriteACValueIndex.restype = wintypes.DWORD
powr.PowerWriteDCValueIndex.argtypes = powr.PowerWriteACValueIndex.argtypes
powr.PowerWriteDCValueIndex.restype = wintypes.DWORD
powr.PowerSetActiveScheme.argtypes = [wintypes.HANDLE, POINTER(GUID)]
powr.PowerSetActiveScheme.restype = wintypes.DWORD
kern = ctypes.WinDLL("kernel32.dll")
kern.LocalFree.argtypes = [c_void_p]


def power_read():
    """返回 (ac, dc, 当前生效值) 或 None。"""
    scheme = POINTER(GUID)()
    if powr.PowerGetActiveScheme(None, byref(scheme)) != 0 or not scheme:
        return None
    try:
        ac, dc = wintypes.DWORD(0), wintypes.DWORD(0)
        r1 = powr.PowerReadACValueIndex(None, scheme, byref(SUBGROUP_VIDEO),
                                        byref(SETTING_BRIGHTNESS), byref(ac))
        r2 = powr.PowerReadDCValueIndex(None, scheme, byref(SUBGROUP_VIDEO),
                                        byref(SETTING_BRIGHTNESS), byref(dc))
        if r1 != 0 or r2 != 0:
            return None
        return ac.value, dc.value
    finally:
        kern.LocalFree(ctypes.cast(scheme, c_void_p))


def wmi_read():
    try:
        import win32com.client
        loc = win32com.client.GetObject("winmgmts:\\\\.\\root\\wmi")
        rows = loc.ExecQuery("SELECT CurrentBrightness FROM WmiMonitorBrightness")
        return [int(r.CurrentBrightness) for r in rows]
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {type(exc).__name__}: {exc}"


def dxva2_probe():
    try:
        dxva = ctypes.WinDLL("dxva2.dll")
        user32 = ctypes.WinDLL("user32.dll")
        hmon = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
        count = wintypes.DWORD(0)
        if not dxva.GetNumberOfPhysicalMonitorsFromHMONITOR(hmon, byref(count)) or count.value == 0:
            return "no physical monitor handle"
        class PHYSICAL_MONITOR(ctypes.Structure):
            _fields_ = [("h", wintypes.HANDLE), ("desc", wintypes.WCHAR * 128)]
        arr = (PHYSICAL_MONITOR * count.value)()
        if not dxva.GetPhysicalMonitorsFromHMONITOR(hmon, count.value, arr):
            return "enum failed"
        try:
            mn, mx, cur = wintypes.DWORD(0), wintypes.DWORD(0), wintypes.DWORD(0)
            ok = dxva.GetMonitorBrightness(arr[0].h, byref(mn), byref(cur), byref(mx))
            return (f"ok cur={cur.value} range={mn.value}-{mx.value}" if ok
                    else "GetMonitorBrightness failed (DDC/CI 不支持)")
        finally:
            dxva.DestroyPhysicalMonitors(count.value, arr)
    except Exception as exc:  # noqa: BLE001
        return f"unavailable: {type(exc).__name__}: {exc}"


def main():
    print("== 1. Power API（Windows 亮度滑杆同源）")
    r = power_read()
    print(f"   结果: {r}")
    check("Power API 读到亮度值", r is not None, str(r))
    if r:
        check("亮度值在 0-100", 0 <= r[0] <= 100 and 0 <= r[1] <= 100, str(r))

    print("== 2. WMI WmiMonitorBrightness（对照）")
    w = wmi_read()
    print(f"   结果: {w}")
    check("WMI 探测未崩溃", True, str(w)[:120])

    print("== 3. dxva2 / DDC-CI")
    d = dxva2_probe()
    print(f"   结果: {d}")

    print(f"PROBE pass={PASS} fail={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
