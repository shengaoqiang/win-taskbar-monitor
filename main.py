"""任务栏内嵌资源监控：主程序。

组成：
- TaskbarStrip：贴任务栏左端的透明置顶条，两行八项读数（CPU/内存/GPU/显存/读写/网速）。
- DetailPanel：托盘唤出的详情浮层（CPU/GPU 趋势卡 + 每核心热力格 + 内存/磁盘/网络分区卡）。
- TrayApp：QSystemTrayIcon + 右键菜单，作为"嵌在任务栏"之外的控制入口。

运行：
    python main.py
"""
from __future__ import annotations

import sys
import os
import ctypes
from ctypes import wintypes
from PySide6.QtCore import Qt, QTimer, QPoint, QPointF, QRect, QRectF
from PySide6.QtGui import (
    QActionGroup, QColor, QIcon, QPainter, QPen, QBrush, QPolygonF,
    QFont, QFontMetrics,
)
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame,
    QSystemTrayIcon, QMenu,
)

import autostart
import config
from collector import Collector
from widgets import Sparkline, MeterBar, CoreGrid
from taskbar_pos import TaskbarInfo

# Win32 强制置顶：第三方窗口设了 StaysOnTop 也未必压得过 explorer 的任务栏，
# 用 SetWindowPos(HWND_TOPMOST) 周期性把内嵌条重新顶到最前最稳。
# ⚠️ 改 z-order 必须带 SWP_NOMOVE|SWP_NOSIZE，否则 x,y,cx,cy 会按 0 生效，
# 把窗口缩成 0×0 并挪到 (0,0)——只动层级，不动位置大小。
HWND_TOPMOST = ctypes.c_void_p(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_FLAGS = SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
_user32 = ctypes.windll.user32 if os.name == "nt" else None


def force_topmost(widget: QWidget) -> None:
    """把 QWidget 对应的 HWND 强制置顶（不改位置/大小、不抢焦点）。"""
    if _user32 is None:
        return
    h = int(widget.winId())
    _user32.SetWindowPos(h, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)


# refresh_topmost 用到的原型。GetWindow 返回 HWND（64 位指针），
# 不设 restype 会被默认 c_int 截断句柄，z 序遍历直接跑飞。
_GWLP_HWNDPARENT = -8
_GWLP_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008
_GW_HWNDPREV = 3      # 3 = z 序中更靠前的窗口；2 是 GW_HWNDNEXT（更靠后），勿混
_RECT = wintypes.RECT
if _user32 is not None:
    _user32.GetWindow.restype = ctypes.c_void_p
    _user32.GetWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    _user32.GetWindowLongW.restype = ctypes.c_ssize_t
    _user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
    _user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
    _user32.FindWindowW.restype = ctypes.c_void_p
    _user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
    # 32 位 Python 没有 *Ptr 版本，回退同名函数（32 位下位宽一致）
    _get_owner = getattr(_user32, "GetWindowLongPtrW", _user32.GetWindowLongW)
    _set_owner = getattr(_user32, "SetWindowLongPtrW", _user32.SetWindowLongW)
    _get_owner.restype = ctypes.c_void_p
    _get_owner.argtypes = [ctypes.c_void_p, ctypes.c_int]
    _set_owner.restype = ctypes.c_void_p
    _set_owner.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
else:
    _get_owner = _set_owner = None


def bind_to_taskbar(widget: QWidget) -> bool:
    """把条挂成任务栏的 owned window。

    Windows 保证 owned window 始终显示在 owner 之上，所以 explorer 无论怎么
    把任务栏提到 z 序最前，条都不会被盖住——这是消除"闪一下"的关键。

    ⚠️ Qt 在 hide/show（或改 windowFlags）时会重建 HWND，owner 关系随之丢失，
    所以每次显示后都要重新绑，运行中也要周期性校验。
    """
    if _user32 is None or _set_owner is None:
        return False
    tb = _user32.FindWindowW("Shell_TrayWnd", None)
    if not tb:
        return False
    me = int(widget.winId())
    if _get_owner(me, _GWLP_HWNDPARENT) == tb:
        return True
    _set_owner(me, _GWLP_HWNDPARENT, tb)
    return True


def _rects_overlap(a, b) -> bool:
    """两个物理像素 RECT 是否相交。"""
    return not (a.right <= b.left or b.right <= a.left
                or a.bottom <= b.top or b.bottom <= a.top)


def refresh_topmost(widget: QWidget) -> None:
    """只在真被压住时才 SetWindowPos。

    ⚠️ 任务栏(Shell_TrayWnd)是真正的遮挡源：explorer 在点击别处/切换窗口时
    会把它提到 z 序最前，条立刻被盖住。所以任务栏必须算作遮挡，不能排除。
    """
    if _user32 is None:
        return
    me = int(widget.winId())
    if not (_user32.GetWindowLongW(me, _GWLP_EXSTYLE) & _WS_EX_TOPMOST):
        force_topmost(widget)
        return
    mine = _RECT()
    _user32.GetWindowRect(me, ctypes.byref(mine))
    cur = me
    while True:
        cur = _user32.GetWindow(cur, _GW_HWNDPREV)
        if not cur:
            return
        # 置顶窗口整体排在非置顶之前，遇到非置顶说明再往前也不可能被盖
        if not (_user32.GetWindowLongW(cur, _GWLP_EXSTYLE) & _WS_EX_TOPMOST):
            return
        if not _user32.IsWindowVisible(cur):
            continue
        r = _RECT()
        _user32.GetWindowRect(cur, ctypes.byref(r))
        if _rects_overlap(mine, r):
            force_topmost(widget)
            return



class Palette:
    # 深色：面板压暗、卡片提亮，靠明度差把分区分开；浅色相反
    DARK_BG = "#15161a"
    DARK_BG_SOFT = "#23252c"
    LIGHT_BG = "#f3f4f6"
    LIGHT_BG_SOFT = "#e4e6ea"
    # 各指标强调色（sparkline + 数值），分深浅两套：
    #   深色任务栏用提亮色，浅色任务栏用加深色，避免浅色底下发飘看不清。
    ACCENT = {
        "dark": {"CPU": "#ff5c5c", "MEM": "#4cc2ff", "NET": "#4ec9b0", "DISK": "#ff9a3c"},
        "light": {"CPU": "#d63636", "MEM": "#1e8fd0", "NET": "#2a9d7f", "DISK": "#e07b1a"},
    }
    # 数值 / 标签文字色
    VALUE = {"dark": "#eef0f5", "light": "#14161c"}
    # 深色下的灰字必须比浅色下亮得多：#8a8d97 压在 #23252c 上只有 4:1，
    # 8pt 小字会糊成一片；#a8aebb 能到 6.5:1
    LABEL = {"dark": "#a8aebb", "light": "#5f6470"}
    # 条上的标签是重字（比数值更重更深），接近系统托盘自带的状态栏文字观感
    LABEL_BOLD = {"dark": "#eef0f4", "light": "#20222a"}
    # 阈值配色：绿=正常、橙=偏高、红=过高
    STATE = {
        "dark": {"green": "#4ade80", "orange": "#ffb84d", "red": "#ff6b6b"},
        "light": {"green": "#0f9d58", "orange": "#c26f00", "red": "#e02020"},
    }


def _system_theme_is_dark() -> bool:
    """读 Windows 注册表 AppsUseLightTheme：1=浅色,0=深色。失败默认深色。"""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                              r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        v, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return (int(v) == 0)
    except Exception:
        return True


def taskbar_bg_is_dark():
    """读任务栏(Shell_TrayWnd)背景刷的实际颜色，判断深浅。检测失败返回 None。

    非直觉点：左侧条压在任务栏上，文字/强调色要匹配任务栏【实际底色】而不是
    系统外观——Win11 任务栏可能是深色 Mica 而系统外观设了浅色，反之亦然，
    跟错就会"发灰看不清"。
    """
    try:
        import struct
        u32 = ctypes.windll.user32
        gdi = ctypes.windll.gdi32
        h = u32.FindWindowW("Shell_TrayWnd", None)
        if not h:
            return None
        brush = u32.GetClassLongW(h, -31, 0)  # GCLP_HBRBACKGROUND
        if not brush:
            return None
        buf = ctypes.create_string_buffer(8)
        gdi.GetObjectW(brush, 8, buf)  # BRUSH: COLORREF(4) + lbStyle(4)
        colorref = struct.unpack_from("<I", buf.raw, 0)[0]
        r, g, b = (colorref >> 16) & 0xFF, (colorref >> 8) & 0xFF, colorref & 0xFF
        lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
        return lum < 128
    except Exception:
        return None


def _detect_default_dark() -> bool:
    """默认深浅：优先任务栏实际底色，检测不到再回退系统外观。"""
    d = taskbar_bg_is_dark()
    if d is not None:
        return d
    return _system_theme_is_dark()


class DetailPanel(QWidget):
    """托盘唤出的详情面板：CPU/GPU 双卡 + 每核心热力格 + 内存/磁盘/网络分区卡。

    非直觉点：
    - 无边框 + 置顶 + Qt.Tool：面板是临时看一眼的浮层，不该占任务栏按钮。
    - 拖动只认顶部 34px，否则在卡片上拖动会误移整个面板。
    - 字号字重走 QFont、颜色走 QSS：QSS 的 font-size 在不同 DPI 下会跳，
      而颜色要能一处切主题，两边分开控制最稳。
    """

    WIDTH = 384
    MAX_PARTS = 3          # 磁盘卡最多显示的分区行数

    def __init__(self, collector: Collector):
        super().__init__()
        self._col = collector
        self._drag_pos: QPoint | None = None
        self._is_dark = True
        self._state = Palette.STATE["dark"]
        self._caps: list[QLabel] = []   # 各卡标题，统一刷灰字色
        self.on_moved = None      # 拖动结束回调，由 TrayApp 注入用于存位置

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                            | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedWidth(self.WIDTH)

        self._body = QFrame()
        self._body.setObjectName("panelBody")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._body)
        inner = QVBoxLayout(self._body)
        inner.setContentsMargins(12, 10, 12, 10)
        inner.setSpacing(8)

        self._build_header(inner)
        self._build_dual(inner)
        self._build_cores(inner)
        self._build_mem(inner)
        self._build_disk(inner)
        self._build_net(inner)
        self._build_footer(inner)

        self.apply_theme(True)

    # ---- 构建 ----
    def _mk(self, text: str, pt: float, weight=QFont.Normal) -> QLabel:
        lb = QLabel(text)
        f = QFont(lb.font())
        f.setPointSizeF(pt)
        f.setWeight(weight)
        lb.setFont(f)
        return lb

    def _card(self):
        f = QFrame()
        f.setObjectName("card")
        lay = QVBoxLayout(f)
        lay.setContentsMargins(10, 7, 10, 7)
        lay.setSpacing(4)
        return f, lay

    def _build_header(self, inner):
        self._title = self._mk("资源监控", 12.5, QFont.Bold)
        self._uptime = self._mk("", 8.5)
        self._close = self._mk("✕", 10)
        self._close.setFixedWidth(14)
        self._close.mousePressEvent = lambda _e: self.close()

        row = QHBoxLayout()
        row.setSpacing(8)
        row.addWidget(self._title, 0, Qt.AlignVCenter)
        row.addWidget(self._uptime, 0, Qt.AlignBottom)
        row.addStretch(1)
        row.addWidget(self._close, 0, Qt.AlignTop)
        inner.addLayout(row)

    def _build_dual(self, inner):
        row = QHBoxLayout()
        row.setSpacing(9)
        self._cpu_val, self._cpu_spark, self._cpu_sub = self._dual_card(row, "CPU")
        self._gpu_val, self._gpu_spark, self._gpu_sub = self._dual_card(row, "GPU")
        inner.addLayout(row)

    def _dual_card(self, row, name):
        card, lay = self._card()
        top = QHBoxLayout()
        top.setSpacing(4)
        cap = self._mk(name, 9)
        self._caps.append(cap)
        val = self._mk("--", 17, QFont.Bold)
        top.addWidget(cap, 0, Qt.AlignBottom)
        top.addStretch(1)
        top.addWidget(val, 0, Qt.AlignBottom)
        lay.addLayout(top)

        spark = Sparkline(upper=100.0, color="#0f9d58", width=1.6,
                          fill=True, grid=True, dot=True)
        # 固定高度：Sparkline 是 Expanding，不锁死的话 sizeHint 会把整张卡撑到 200px+
        spark.setFixedHeight(34)
        lay.addWidget(spark)

        sub = self._mk("", 8.5)
        lay.addWidget(sub)
        row.addWidget(card, 1)
        return val, spark, sub

    def _build_cores(self, inner):
        card, lay = self._card()
        head = QHBoxLayout()
        cap = self._mk("核心占用", 9)
        self._caps.append(cap)
        self._core_sum = self._mk("", 8.5)
        head.addWidget(cap, 0, Qt.AlignBottom)
        head.addStretch(1)
        head.addWidget(self._core_sum, 0, Qt.AlignBottom)
        lay.addLayout(head)

        self._cores = CoreGrid(rows=1)
        lay.addWidget(self._cores)
        inner.addWidget(card)

    def _build_mem(self, inner):
        card, lay = self._card()
        top = QHBoxLayout()
        cap = self._mk("内存", 9)
        self._caps.append(cap)
        self._mem_val = self._mk("--", 13, QFont.DemiBold)
        top.addWidget(cap, 0, Qt.AlignBottom)
        top.addStretch(1)
        top.addWidget(self._mem_val, 0, Qt.AlignBottom)
        lay.addLayout(top)

        self._mem_bar = MeterBar(7)
        lay.addWidget(self._mem_bar)

        self._mem_sub = self._mk("", 8.5)
        lay.addWidget(self._mem_sub)
        inner.addWidget(card)

    def _build_disk(self, inner):
        card, lay = self._card()
        top = QHBoxLayout()
        cap = self._mk("磁盘", 9)
        self._caps.append(cap)
        self._io_val = self._mk("", 8.5)
        top.addWidget(cap, 0, Qt.AlignBottom)
        top.addStretch(1)
        top.addWidget(self._io_val, 0, Qt.AlignBottom)
        lay.addLayout(top)

        self._part_rows = []
        for _ in range(self.MAX_PARTS):
            # 一行到底：盘符 / 进度条撑满 / 百分比 / 容量右对齐
            line = QHBoxLayout()
            line.setSpacing(7)
            name = self._mk("", 9, QFont.DemiBold)
            name.setFixedWidth(22)
            bar = MeterBar(5)
            pct = self._mk("", 8.5)
            pct.setFixedWidth(30)
            pct.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            size = self._mk("", 8.5)
            size.setFixedWidth(80)
            size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            line.addWidget(name)
            line.addWidget(bar, 1)
            line.addWidget(pct)
            line.addWidget(size)
            lay.addLayout(line)
            self._part_rows.append((name, pct, size, bar))
        inner.addWidget(card)

    def _build_net(self, inner):
        card, lay = self._card()
        top = QHBoxLayout()
        cap = self._mk("网络", 9)
        self._caps.append(cap)
        self._net_val = self._mk("--", 13, QFont.DemiBold)
        top.addWidget(cap, 0, Qt.AlignBottom)
        top.addStretch(1)
        top.addWidget(self._net_val, 0, Qt.AlignBottom)
        lay.addLayout(top)

        # 分网卡压成一行：行数固定，否则有无流量时卡片高度会跳、整个面板跟着抖
        self._nic_line = self._mk("", 8.5)
        self._nic_line.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        lay.addWidget(self._nic_line)
        inner.addWidget(card)

    def _build_footer(self, inner):
        self._foot = self._mk("", 8)
        inner.addWidget(self._foot)
        # 兜住多余高度：没有这个 stretch，窗口一旦比内容高，多出来的空间会
        # 被平均塞进各张卡，卡片被撑高、文字之间出现大片空白。
        inner.addStretch(1)

    # ---- 主题 ----
    def apply_theme(self, is_dark: bool):
        self._is_dark = is_dark
        key = "dark" if is_dark else "light"
        bg = Palette.DARK_BG if is_dark else Palette.LIGHT_BG
        card = Palette.DARK_BG_SOFT if is_dark else Palette.LIGHT_BG_SOFT
        text = Palette.VALUE[key]
        dim = Palette.LABEL[key]
        self._state = Palette.STATE[key]

        self._body.setStyleSheet(f"""
            QFrame#panelBody{{background:{bg};border-radius:12px;}}
            QFrame#card{{background:{card};border-radius:8px;}}
            QLabel{{color:{dim};}}
        """)
        # 主文字色（数值/名称）与灰文字色（说明）分两组刷，避免逐个判断
        for lb in (self._title, self._mem_val, self._net_val):
            lb.setStyleSheet(f"color:{text};")
        for lb in (self._uptime, self._close, self._core_sum, self._mem_sub,
                   self._io_val, self._foot, self._cpu_sub, self._gpu_sub,
                   self._nic_line, *self._caps):
            lb.setStyleSheet(f"color:{dim};")
        for name, pct, size, _ in self._part_rows:
            name.setStyleSheet(f"color:{text};")
            pct.setStyleSheet(f"color:{dim};")
            size.setStyleSheet(f"color:{dim};")

        self._cpu_spark.set_color(self._state["green"])
        self._gpu_spark.set_color(self._state["green"])
        # 深色卡片上同样透明度会厚重得多，压住底部文字，所以单独调淡
        fill_alpha = 62 if is_dark else 100
        self._cpu_spark.set_fill_alpha(fill_alpha)
        self._gpu_spark.set_fill_alpha(fill_alpha)
        # 进度条空槽：浅色卡片上必须够深才看得出"整条有多长"，
        # 否则只剩一小截填充，会被误读成"条很短"
        track = "#1affffff" if is_dark else "#2a000000"
        self._mem_bar.set_track(track)
        for _, _, _, bar in self._part_rows:
            bar.set_track(track)
        self.refresh()
        self.adjustSize()

    def _pct_color(self, v) -> str:
        if v is None:
            return self._state["green"]
        if v >= 90:
            return self._state["red"]
        if v >= 70:
            return self._state["orange"]
        return self._state["green"]

    # ---- 刷新 ----
    def refresh(self):
        s = self._col.last
        if s.cpu is None:      # 首帧还没采到，别拿 None 去格式化
            return
        hist = self._col.get_history()
        st = self._state

        self._uptime.setText("已运行 " + _fmt_uptime(s.uptime))

        # CPU / GPU 双卡
        c = self._pct_color(s.cpu)
        self._cpu_val.setText(f"{s.cpu:.0f}%")
        self._cpu_val.setStyleSheet(f"color:{c};")
        cpu_hist = [x.cpu for x in hist]
        # 上限按峰值浮动：固定 100 时低负载曲线会贴在底边，看不出起伏
        self._cpu_spark.set_color(c)          # 曲线跟数值同色，高负载整卡转红
        self._cpu_spark.set_data(cpu_hist,
                                 upper=max(40.0, max(cpu_hist or [0]) * 1.15))
        ncore = len(s.cpu_cores or [])
        self._cpu_sub.setText(f"{ncore} 核心" if ncore else "")

        g = self._pct_color(s.gpu_pct)
        self._gpu_val.setText("--" if s.gpu_pct is None else f"{s.gpu_pct:.0f}%")
        self._gpu_val.setStyleSheet(f"color:{g};")
        gpu_hist = [x.gpu_pct or 0.0 for x in hist]
        self._gpu_spark.set_color(g)
        self._gpu_spark.set_data(gpu_hist,
                                 upper=max(40.0, max(gpu_hist or [0]) * 1.15))
        self._gpu_sub.setText(
            "显存 --" if s.vram_gb is None else f"显存 {s.vram_gb:.1f} GB")

        # 每核心热力格
        cores = s.cpu_cores or []
        self._cores.set_rows(1 if len(cores) <= 16 else 2)
        self._cores.set_data([self._pct_color(v) for v in cores])
        if cores:
            hi = max(cores)
            self._core_sum.setText(f"最高 {hi:.0f}% · 共 {len(cores)}")

        # 内存
        self._mem_val.setText(f"{s.mem_pct:.0f}%")
        self._mem_val.setStyleSheet(f"color:{self._pct_color(s.mem_pct)};")
        self._mem_bar.set_parts([(s.mem_pct / 100.0, self._pct_color(s.mem_pct))])
        self._mem_sub.setText(
            f"{s.mem_used_gb:.1f} / {s.mem_total_gb:.1f} GB"
            f"　可用 {s.mem_avail_gb:.1f} GB")

        # 磁盘分区
        parts = s.partitions or []
        for i, (name_lb, pct_lb, size_lb, bar) in enumerate(self._part_rows):
            if i < len(parts):
                name, used, total, pct = parts[i]
                name_lb.setText(name)
                pct_lb.setText(f"{pct:.0f}%")
                pct_lb.setStyleSheet(f"color:{self._pct_color(pct)};")
                size_lb.setText(f"{used:.0f} / {total:.0f} GB")
                bar.set_parts([(pct / 100.0, self._pct_color(pct))])
                for w in (name_lb, pct_lb, size_lb, bar):
                    w.setVisible(True)
            else:
                for w in (name_lb, pct_lb, size_lb, bar):
                    w.setVisible(False)
        self._io_val.setText(
            f"读 {Collector.fmt_speed(s.disk_read)}"
            f"　写 {Collector.fmt_speed(s.disk_write)}")

        # 网络
        self._net_val.setText(
            f"↓ {Collector.fmt_speed_compact(s.net_down)}"
            f"  ↑ {Collector.fmt_speed_compact(s.net_up)}")
        self._net_val.setStyleSheet(f"color:{st['green']};")
        nics = s.net_nics or []
        if nics:
            self._nic_line.setText(" · ".join(
                f"{name} ↓{Collector.fmt_speed_compact(down)}"
                f" ↑{Collector.fmt_speed_compact(up)}"
                for name, up, down in nics[:2]))
        else:
            self._nic_line.setText("无活动网卡")

        self._foot.setText(_system_line(s))

        # 分区/网卡行数变化会改变内容高度，面板是浮层不能留白，跟着收一次。
        # 不能只在可见时收：面板首次是隐藏状态下刷新的，等用户打开时会带着旧高度。
        self.adjustSize()

    # ---- 拖动 ----
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.pos().y() < 34:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)
            e.accept()

    def mouseReleaseEvent(self, e):
        if self._drag_pos is not None and self.on_moved is not None:
            self.on_moved(self.pos())
        self._drag_pos = None


def _fmt_uptime(sec) -> str:
    sec = int(sec or 0)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d} 天 {h} 小时"
    if h:
        return f"{h} 小时 {m} 分"
    return f"{m} 分"


def _system_line(s) -> str:
    """底栏一行：系统 + 内存容量 + CPU 核数，够看又不多占地方。"""
    import platform
    ncore = len(s.cpu_cores or [])
    bits = [f"Windows {platform.release()}", f"{s.mem_total_gb:.0f} GB"]
    if ncore:
        bits.append(f"{ncore} 线程")
    return " · ".join(bits)


class TaskbarStrip(QWidget):
    """贴任务栏最左的透明置顶条 = 两行 × 四列读数（8 项指标）。

    非直觉点：
    - 透明(WA_TranslucentBackground) + 无边框 + 常驻置顶，整条只有一个自绘的
      paintEvent，没有任何子控件，所以不会遮挡任务栏其他内容。
    - 定位靠 taskbar_pos.detect()：贴任务栏左端、高度=任务栏高度、宽度按内容算死。
    - 靠 bind_to_taskbar() 挂成任务栏的 owned window，才不会被 explorer 盖住。
    - 整条可拖动（只响应拖动，不响应点击）。
    - 温度取不到（MSAcpi 要管理员、Thermal Zone 计数器在本机返回空），
      所以两行里放的全是能实测的指标，没有用假数据占位。
    """

    LABEL_PT = 8.5        # 标签字号（重字，参考系统状态栏观感）
    VALUE_PT = 11.0       # 数值字号
    GAP_LABEL = 4         # 标签右缘到数值（紧凑：只留一口气）
    GAP_COL = 10          # 相邻两列之间
    PAD_X = 7             # 左右内边距
    ICON = 12             # 网速方向图标边长

    # 两行 × 四列，行优先。None = 该格画方向图标而不是文字标签。
    CELLS = (
        ("CPU", "cpu"), ("GPU", "gpu"), ("读", "rd"), (None, "up"),
        ("内存", "mem"), ("显存", "vram"), ("写", "wr"), (None, "dn"),
    )
    # 每格数值的最坏宽度样本：列宽按它算死，数字换位数不会推着整行跳
    SAMPLES = ("100%", "100%", "99.9M", "99.9M",
               "100%", "99.9G", "99.9M", "99.9M")

    def __init__(self, collector: Collector):
        super().__init__()
        self._col = collector
        self._drag_pos: QPoint | None = None
        self._bounds: QRect | None = None   # 可拖动范围 = 任务栏矩形
        self.on_moved = None      # 拖动结束回调，由 TrayApp 注入用于存位置

        # 关键：用普通 frameless 顶层窗口（不加 Qt.Tool）。
        # Qt.Tool 的语义是"不常驻前台"，点击其他区域/切焦点时会被 Windows 降序隐藏，
        # 这正是"点别处就消失"的根因。去掉 Tool 后窗口失焦也保持可见。
        # 仍保留 StaysOnTop + force_topmost 压在任务栏之上。
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        # 永不抢焦点/不激活：点击别处时窗口不经历 active->inactive 切换，
        # 避免 Qt 重绘透明层造成"闪一下"
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)

        # 自绘两行读数：不建子控件。48px 里塞两行中英混排，QLabel+布局控不住
        # 垂直居中（差 1~2px）和数值换位数的左右抖动，只能自己算坐标画。
        self._values = ["--"] * len(self.CELLS)
        self._colors = ["#e8e9ed"] * len(self.CELLS)
        self._label_color = "#eef0f4"
        self._lf = QFont()
        self._lf.setPointSizeF(self.LABEL_PT)
        self._lf.setWeight(QFont.DemiBold)
        self._vf = QFont()
        self._vf.setPointSizeF(self.VALUE_PT)
        self._vf.setWeight(QFont.DemiBold)
        # 等宽数字：否则 0/1/8 宽度不同，每秒刷新时数值会左右抖
        self._vf.setFeature(QFont.Tag("tnum"), 1)

        self._apply_theme(False)      # 条固定浅色，不跟系统/面板主题
        # 整个条可拖动（不点面板）：见下方 mouse* 事件

    # ---- 拖动：只允许在任务栏内横向移动 ----
    def set_bounds(self, rect: QRect):
        """限定可拖动范围。垂直方向一律锁死在任务栏内，不允许拖出任务栏。"""
        self._bounds = rect

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            e.accept()

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and e.buttons() & Qt.LeftButton:
            p = e.globalPosition().toPoint() - self._drag_pos
            if self._bounds is not None:
                r = self._bounds
                # QRect.right() 是闭区间最后一个像素，减宽度要 +1
                max_x = max(r.left(), r.right() - self.width() + 1)
                p.setX(min(max(p.x(), r.left()), max_x))
                p.setY(r.top())      # 高度锁死在任务栏
            self.move(p)
            e.accept()

    def mouseReleaseEvent(self, e):
        if self._drag_pos is not None and self.on_moved is not None:
            self.on_moved(self.pos())
        self._drag_pos = None

    def _layout(self):
        """返回 (列起点, 标签宽, 数值起点, 总宽)。

        列宽按两行里更宽的那格算死，所以某格数值换位数只会在本列内变宽，
        不会推着后面几列左右跳。
        """
        lfm = QFontMetrics(self._lf)
        vfm = QFontMetrics(self._vf)
        ncol = len(self.CELLS) // 2
        col_x, label_w, val_x = [], [], []
        x = self.PAD_X
        for c in range(ncol):
            lw = max(self.ICON if self.CELLS[r][0] is None
                     else lfm.horizontalAdvance(self.CELLS[r][0])
                     for r in (c, c + ncol))
            vw = max(vfm.horizontalAdvance(self.SAMPLES[r])
                     for r in (c, c + ncol))
            col_x.append(x)
            label_w.append(lw)
            val_x.append(x + lw + self.GAP_LABEL)
            x += lw + self.GAP_LABEL + vw + self.GAP_COL
        return col_x, label_w, val_x, x - self.GAP_COL + self.PAD_X

    def _rate_color(self, bps, active: str) -> str:
        """空闲速率不染色：一排 0B 全是绿的太吵，灰掉才看得出"没有活动"。"""
        return self._dim if bps < 1024 else active

    def _pct_color(self, v):
        """阈值配色：正常绿、偏高橙、过高红；取不到值不染色。"""
        if v is None:
            return self._dim
        if v >= 90:
            return self._state["red"]
        if v >= 70:
            return self._state["orange"]
        return self._state["green"]

    def _apply_theme(self, is_dark: bool):
        """条固定浅色（调用方一律传 False），dark 分支留着以便以后想跟随时切回。"""
        key = "dark" if is_dark else "light"
        self._label_color = Palette.LABEL_BOLD[key]
        self._state = Palette.STATE[key]
        self._dim = Palette.LABEL[key]
        self.update()

    def set_size(self, height: int):
        """高度跟随任务栏，宽度按两行最坏内容算死。"""
        _, _, _, w = self._layout()
        self.setFixedSize(w, max(height, 30))

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.setRenderHint(QPainter.Antialiasing, True)
        col_x, label_w, val_x, _ = self._layout()
        ncol = len(self.CELLS) // 2
        rh = self.height() / 2.0

        for i, (label, _) in enumerate(self.CELLS):
            c, row = i % ncol, i // ncol
            y0 = row * rh
            # 两行各自在本行内垂直居中；共用一个基线会串行
            if label is None:
                self._draw_net_icon(p, col_x[c], y0, rh, up=(row == 0))
            else:
                p.setFont(self._lf)
                p.setPen(QColor(self._label_color))
                p.drawText(QRectF(col_x[c], y0, label_w[c], rh),
                           Qt.AlignLeft | Qt.AlignVCenter, label)
            p.setFont(self._vf)
            p.setPen(QColor(self._colors[i]))
            p.drawText(QRectF(val_x[c], y0, self.width() - val_x[c], rh),
                       Qt.AlignLeft | Qt.AlignVCenter, self._values[i])
        p.end()

    def _draw_net_icon(self, p: QPainter, x: float, y0: float, rh: float, up: bool):
        """圆角方框 + 方向箭头。用画的不用字符：↑↓ 在小字号下偏细、基线也飘。"""
        s = self.ICON
        top = y0 + (rh - s) / 2.0
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(self._label_color), 1.2))
        p.drawRoundedRect(QRectF(x, top, s, s), 3.5, 3.5)

        cx = x + s / 2.0
        head, tail = ((top + 3.6, top + s - 3.6) if up
                      else (top + s - 3.6, top + 3.6))
        p.setPen(QPen(QColor(self._label_color), 1.4,
                      Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(cx, head), QPointF(cx, tail))
        tip = head
        wing = 2.5 if up else -2.5
        p.drawLine(QPointF(cx, tip), QPointF(cx - 2.5, tip + wing))
        p.drawLine(QPointF(cx, tip), QPointF(cx + 2.5, tip + wing))

    def refresh(self):
        s = self._col.last
        gpu = "--" if s.gpu_pct is None else f"{s.gpu_pct:.0f}%"
        if s.vram_gb is None:
            vram = "--"
        elif s.vram_gb < 99.95:
            vram = f"{s.vram_gb:.1f}G"
        else:
            vram = f"{s.vram_gb:.0f}G"
        fmt = Collector.fmt_speed_compact
        self._values = [
            f"{s.cpu:.0f}%", gpu, fmt(s.disk_read), fmt(s.net_up),
            f"{s.mem_pct:.0f}%", vram, fmt(s.disk_write), fmt(s.net_down),
        ]
        g, o = self._state["green"], self._state["orange"]
        self._colors = [
            self._pct_color(s.cpu), self._pct_color(s.gpu_pct),
            self._rate_color(s.disk_read, g), self._rate_color(s.net_up, g),
            self._pct_color(s.mem_pct), g,
            self._rate_color(s.disk_write, o), self._rate_color(s.net_down, g),
        ]
        self.update()


class TrayApp:
    """系统托盘 + 装配：采集定时器、内嵌条、详细面板、托盘菜单。"""

    REFRESH_MS = 1000

    def __init__(self, app: QApplication):
        self._app = app
        self._cfg = config.load()
        self._col = Collector()
        self._panel = DetailPanel(self._col)
        self._strip = TaskbarStrip(self._col)
        self._strip.on_moved = self._save_strip_pos
        self._panel.on_moved = self._save_panel_pos

        # 任务栏定位：默认贴最左，拖过就沿用保存的位置
        screen = app.primaryScreen()
        info, _ = TaskbarInfo.detect(screen)
        self._apply_taskbar(info)
        self._strip.move(*self._fit_strip_pos(self._cfg.get("strip_pos")))

        # 主题：按保存的模式（follow=跟任务栏实际底色，否则锁定深浅）
        mode = self._cfg.get("theme_mode", "follow")
        self._theme_follow_system = (mode == "follow")
        self._theme_dark = (_detect_default_dark() if self._theme_follow_system
                            else mode == "dark")
        self._theme_check = 0      # 跟随模式的复检计数（见 _on_tick）
        self._strip._apply_theme(False)       # 条固定浅色，不随主题
        self._panel.apply_theme(self._theme_dark)

        self._build_tray()

        # 数据采集定时器
        self._timer = QTimer(self._strip)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(self.REFRESH_MS)

        # 层级守护：50ms。explorer 把任务栏提到最前时条会被盖住，
        # 1 秒才顶一次的话空档近 1 秒 = 明显"消失/闪一下"；50ms 顶回人眼几乎无感。
        self._guard = QTimer(self._strip)
        self._guard.timeout.connect(self._guard_zorder)
        self._guard.start(50)

        self._panel.show()
        self._panel.hide()  # 先 show 定位尺寸再 hide
        ppos = self._cfg.get("panel_pos")
        if ppos:
            self._panel.move(*ppos)

        # 数字条按保存的显隐状态；显示时绑成任务栏的 owned window（永不被覆盖）
        if self._cfg.get("strip_visible", True):
            self._strip.show()
            bind_to_taskbar(self._strip)
            force_topmost(self._strip)
        self._refresh_strip_menu()

        # 屏幕分辨率/缩放变化后重新贴位
        screen.geometryChanged.connect(self._reposition)
        screen.logicalDotsPerInchChanged.connect(self._reposition)

    # ---- 任务栏几何 / 位置约束 ----
    def _apply_taskbar(self, info):
        """按任务栏几何刷新条的尺寸（宽度按内容自适应）和可拖动范围。"""
        self._tb_rect = QRect(info.left, info.top,
                              info.right - info.left, info.height)
        self._strip.set_size(info.height)
        self._strip.set_bounds(self._tb_rect)

    def _fit_strip_pos(self, pos):
        """位置必须落在任务栏内；越界（换过分辨率/任务栏）就归位到最左。"""
        r = self._tb_rect
        if not pos:
            return r.left(), r.top()
        x = pos[0]
        if x < r.left() or x > r.right() - self._strip.width() + 1:
            return r.left(), r.top()
        return x, r.top()      # 垂直方向一律锁死在任务栏

    # ---- 配置持久化 ----
    def _save_strip_pos(self, pos):
        self._cfg["strip_pos"] = [pos.x(), pos.y()]
        config.save(self._cfg)

    def _save_panel_pos(self, pos):
        # 面板也夹回屏幕内，避免拖到屏幕外找不回来
        geo = self._app.primaryScreen().geometry()
        x = min(max(pos.x(), 0), max(geo.width() - self._panel.width(), 0))
        y = min(max(pos.y(), 0), max(geo.height() - self._panel.height(), 0))
        self._panel.move(x, y)
        self._cfg["panel_pos"] = [x, y]
        config.save(self._cfg)

    def _reposition(self, *_):
        """屏幕分辨率/缩放变了：重算尺寸和可拖动范围，位置越界就归位。"""
        info, _ = TaskbarInfo.detect(self._app.primaryScreen())
        self._apply_taskbar(info)
        self._strip.move(*self._fit_strip_pos(self._cfg.get("strip_pos")))

    def _build_tray(self):
        self._tray = QSystemTrayIcon(self._app)
        self._tray.setIcon(self._make_icon())
        self._tray.setToolTip("资源监控 · 右键打开菜单")

        m = QMenu()
        m.setObjectName("trayMenu")
        self._menu = m   # 样式在 _refresh_theme_menu 里按主题刷

        # 两个开关项：勾选态直接表达"现在开着没"，比"显示/隐藏 xx"更好读
        self._act_strip = m.addAction("数据条")
        self._act_strip.setCheckable(True)
        self._act_strip.triggered.connect(self.toggle_strip)
        self._act_toggle = m.addAction("详情面板")
        self._act_toggle.setCheckable(True)
        self._act_toggle.triggered.connect(self.toggle_panel)

        m.addSeparator()
        # 三个主题项收进子菜单，顶层只留 3 组，不必在一屏菜单里读 6 行
        tm = m.addMenu("主题")
        tm.setObjectName("subMenu")
        self._sub_menu = tm
        grp = QActionGroup(m)
        grp.setExclusive(True)
        self._act_follow = tm.addAction("跟随系统")
        self._act_follow.triggered.connect(lambda: self._set_theme(None))
        self._act_dark = tm.addAction("深色")
        self._act_dark.triggered.connect(lambda: self._set_theme(True))
        self._act_light = tm.addAction("浅色")
        self._act_light.triggered.connect(lambda: self._set_theme(False))
        for a in (self._act_follow, self._act_dark, self._act_light):
            a.setCheckable(True)
            grp.addAction(a)
        self._theme_group = grp

        self._act_autostart = m.addAction("开机自启")
        self._act_autostart.setCheckable(True)
        self._act_autostart.setChecked(autostart.is_enabled())
        self._act_autostart.triggered.connect(self._toggle_autostart)

        m.addSeparator()
        self._act_quit = m.addAction("退出")
        self._act_quit.triggered.connect(self._app.quit)

        self._tray.setContextMenu(m)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()
        self._refresh_theme_menu()

    def _set_theme(self, is_dark):
        """is_dark=None 表示跟随任务栏底色；True/False 表示手动锁定深浅。"""
        if is_dark is None:
            self._theme_follow_system = True
            self._theme_dark = _detect_default_dark()
            self._cfg["theme_mode"] = "follow"
        else:
            self._theme_follow_system = False
            self._theme_dark = bool(is_dark)
            self._cfg["theme_mode"] = "dark" if self._theme_dark else "light"
        config.save(self._cfg)
        self._apply_theme_all()

    def _apply_theme_all(self):
        """把当前深浅同步到面板和托盘菜单（数据条固定浅色，不参与）。"""
        self._strip._apply_theme(False)       # 条固定浅色，不随主题
        self._panel.apply_theme(self._theme_dark)
        self._refresh_theme_menu()

    def _style_menu(self, is_dark: bool):
        """托盘菜单跟着主题走——写死深色在浅色下很突兀。"""
        bg = Palette.DARK_BG if is_dark else Palette.LIGHT_BG
        text, dim = ("#e6e8ee", "#8f95a3") if is_dark else ("#1a1c22", "#666b7a")
        border = "#383c45" if is_dark else "#d9dce2"
        hover = "#30343d" if is_dark else "#e6e9ef"
        sep = "#2a2d34" if is_dark else "#dfe2e8"
        self._menu.setStyleSheet(f"""
            QMenu{{background:{bg};color:{text};border:1px solid {border};
                border-radius:8px;padding:5px;font-size:9.5pt;}}
            QMenu::item{{padding:7px 16px 7px 6px;border-radius:5px;
                color:{text};background:transparent;}}
            QMenu::item:selected{{background:{hover};}}
            QMenu::item:disabled{{color:{dim};}}
            QMenu::separator{{height:1px;background:{sep};margin:4px 8px;}}
            QMenu::indicator{{width:12px;height:12px;}}
            QMenu::right-arrow{{width:9px;height:9px;right:8px;}}
        """)

    def _refresh_theme_menu(self):
        self._style_menu(self._theme_dark)
        # 互斥勾选由 QActionGroup 管，这里只按当前模式把勾打准
        self._act_follow.setChecked(self._theme_follow_system)
        self._act_dark.setChecked(not self._theme_follow_system and self._theme_dark)
        self._act_light.setChecked(not self._theme_follow_system
                                   and not self._theme_dark)
        self._refresh_switch_menu()

    def _refresh_switch_menu(self):
        """两个开关项的勾选态跟着实际显隐走。"""
        self._act_strip.setChecked(self._strip.isVisible())
        self._act_toggle.setChecked(self._panel.isVisible())

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.toggle_panel()

    def _refresh_strip_menu(self):
        self._refresh_switch_menu()

    def _toggle_autostart(self):
        want = self._act_autostart.isChecked()
        if autostart.set_enabled(want):
            return
        self._act_autostart.setChecked(not want)   # 写注册表失败，勾选要跟回实际状态
        self._tray.showMessage("资源监控", "开机自启设置失败，请检查注册表权限",
                               QSystemTrayIcon.Warning, 3000)

    def toggle_strip(self):
        """数字条开关。显示时沿用拖过的位置，没拖过就贴任务栏最左。"""
        if self._strip.isHidden():
            # 任务栏布局可能变过（改缩放/换分辨率），尺寸和可拖动范围都重算
            info, _ = TaskbarInfo.detect(self._app.primaryScreen())
            self._apply_taskbar(info)
            self._strip.move(*self._fit_strip_pos(self._cfg.get("strip_pos")))
            self._strip.show()
            bind_to_taskbar(self._strip)
            force_topmost(self._strip)
        else:
            self._strip.hide()
        self._cfg["strip_visible"] = self._strip.isVisible()
        config.save(self._cfg)
        self._refresh_strip_menu()

    def toggle_panel(self):
        if self._panel.isHidden():
            # 面板隐藏时不刷新，先补一帧，否则打开瞬间是空白、要等下一个 tick。
            # 必须在算位置之前，refresh 里的 adjustSize 会改高度。
            self._panel.refresh()
            pos = self._cfg.get("panel_pos")
            if pos:
                self._panel.move(*pos)       # 沿用上次拖到的位置
            else:
                geo = self._app.primaryScreen().geometry()
                w, h = self._panel.width(), self._panel.height()
                self._panel.move(geo.width() - w - 12, geo.height() - h - 60)
            self._panel.show()
            self._panel.raise_()
            self._panel.activateWindow()
        else:
            self._panel.hide()
        self._refresh_switch_menu()

    def _on_tick(self):
        s = self._col.tick()
        if not self._strip.isHidden():
            self._strip.refresh()
            bind_to_taskbar(self._strip)  # HWND 被 Qt 重建或 explorer 重启后补绑
        if not self._panel.isHidden():
            self._panel.refresh()
        self._tray.setToolTip(
            f"CPU {s.cpu:.0f}%  内存 {s.mem_pct:.0f}%  "
            f"↓{Collector.fmt_speed(s.net_down)}  ↑{Collector.fmt_speed(s.net_up)}"
        )
        # 跟随模式：每 3 秒复检一次任务栏底色，用户切系统深浅后自动跟上
        if self._theme_follow_system:
            self._theme_check += 1
            if self._theme_check % 3 == 0:
                d = _detect_default_dark()
                if d != self._theme_dark:
                    self._theme_dark = d
                    self._apply_theme_all()

    def _guard_zorder(self):
        """高频层级守护：被任务栏压住就立刻顶回去（见 _guard 注释）。"""
        if not self._strip.isHidden():
            refresh_topmost(self._strip)

    def _make_icon(self) -> QIcon:
        """自绘托盘图标：一个小仪表盘圆点。"""
        from PySide6.QtGui import QPixmap
        pm = QPixmap(64, 64)
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setBrush(QBrush(QColor(Palette.ACCENT["dark"]["MEM"])))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(4, 4, 56, 56, 12, 12)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(Palette.ACCENT["dark"]["CPU"]), 4))
        # 一根折线（示意 CPU）
        p.drawPolygon(QPolygonF([
            QPointF(14, 40), QPointF(26, 26), QPointF(38, 34), QPointF(50, 20),
        ]))
        p.end()
        return QIcon(pm)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)  # 关掉面板/条不退出，只留托盘
    app.setStyle("Fusion")
    TrayApp(app)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()



