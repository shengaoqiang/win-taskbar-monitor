"""全屏检测：前台有全屏程序时，数据条要让位。

只用系统状态 SHQueryUserNotificationState，不用窗口几何比大小——
任务栏设成自动隐藏时，随便一个最大化窗口的矩形也铺满屏幕，
按几何判会把条在整个工作期间永久藏掉。

返回值：1 正常、2 全屏程序或演示模式、3 独占全屏 D3D。
"""
from __future__ import annotations

import ctypes
import os

_QUNS_BUSY = 2
_QUNS_D3D_FULL_SCREEN = 3

if os.name == "nt":
    _shell32 = ctypes.windll.shell32
    _shell32.SHQueryUserNotificationState.argtypes = [ctypes.POINTER(ctypes.c_int)]
    _shell32.SHQueryUserNotificationState.restype = ctypes.c_long
else:
    _shell32 = None


def is_fullscreen() -> bool:
    if _shell32 is None:
        return False
    state = ctypes.c_int(0)
    try:
        if _shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
    except Exception:
        return False
    return state.value in (_QUNS_BUSY, _QUNS_D3D_FULL_SCREEN)


def state() -> int:
    """调试用：原始状态值。"""
    if _shell32 is None:
        return -1
    v = ctypes.c_int(0)
    _shell32.SHQueryUserNotificationState(ctypes.byref(v))
    return v.value
