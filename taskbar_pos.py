"""任务栏定位助手。

非直觉点：
- 任务栏是 Win32 窗口 "Shell_TrayWnd"，用 GetWindowRect 拿它的边界，
  条要贴任务栏左端、高度=任务栏高度。
- 只有上下两种任务栏能跟（条是横排的）；任务栏竖在左右时放不下，
  退化到屏幕底边，避免为罕见布局写死逻辑。
- 找不到任务栏窗口（远程/特殊会话）时返回兜底值，调用方直接用。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]


def get_taskbar_rect():
    """返回任务栏 RECT(left, top, right, bottom) 或 None。"""
    try:
        h = user32.FindWindowW("Shell_TrayWnd", None)
        if not h:
            return None
        r = wintypes.RECT()
        if not user32.GetWindowRect(h, ctypes.byref(r)):
            return None
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


class TaskbarInfo:
    """任务栏几何 + 内嵌条放置参数。"""

    def __init__(self, height: int, top: int, left: int, right: int):
        self.height = height
        self.top = top
        self.left = left
        self.right = right

    @classmethod
    def detect(cls, screen):
        """根据任务栏位置与屏幕，算出内嵌条该放哪。

        返回 (TaskbarInfo, 说明)。

        ⚠️ 关键坑：GetWindowRect 返回的是【物理像素】，而 Qt 的 QWidget.move/
        setFixedSize 用【逻辑像素】。高 DPI（如 200%）下不换算会把条甩到
        屏幕外面。这里用 screen.devicePixelRatio() 把物理坐标折算成逻辑坐标。
        """
        dpr = screen.devicePixelRatio() or 1.0
        geo = screen.geometry()  # 逻辑坐标
        sw, sh = geo.width(), geo.height()

        tbr = get_taskbar_rect()
        if tbr and dpr:
            # 物理 -> 逻辑
            l, t, r, b = tbr
            tbr = (l / dpr, t / dpr, r / dpr, b / dpr)

        if tbr:
            l, t, r, b = tbr
            h = int(round(b - t))
            if h >= 20:
                if t >= sh - h - 8:      # 任务栏在底部
                    return cls(h, int(round(t)), int(round(l)), int(round(r))), "bottom"
                if t <= 8:               # 任务栏在顶部
                    return cls(h, int(round(t)), int(round(l)), int(round(r))), "top"

        # 兜底：左右竖排的任务栏放不下横排条，退到屏幕底边，默认高 40
        h = 40
        return cls(h, sh - h, 0, sw), "fallback"
