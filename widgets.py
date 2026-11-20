"""UI 组件：趋势折线 / 进度条 / 核心热力格。

非直觉点：
- Sparkline 只画细折线、不填充：渐变面积压在浅色底上会糊成一块脏色。
- 颜色由上层按主题注入，组件本身不做配色决策。
"""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import (
    QPainter, QColor, QPen, QPolygonF, QPainterPath, QLinearGradient,
)
from PySide6.QtWidgets import QWidget, QFrame, QSizePolicy


class Sparkline(QFrame):
    """趋势图：折线 + 可选面积渐变 / 中位参考线 / 末端圆点。

    纯折线在卡片里太空，面积渐变能让"最近这一段的高低"一眼看出来；
    填充只在实色卡片上用——压在透明任务栏上会糊成一块脏色。
    """

    def __init__(self, upper: float = 100.0, color: str = "#4CC2FF",
                 width: float = 1.3, fill: bool = False, grid: bool = False,
                 dot: bool = False, parent=None):
        super().__init__(parent)
        self._data: list[float] = []
        self._upper = float(upper) if upper > 0 else 1.0
        self._color = QColor(color)
        self._width = width
        self._fill = fill
        self._fill_alpha = 105
        self._grid = grid
        self._dot = dot
        self.setMinimumHeight(9)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        # 垂直必须 Expanding，否则会被 sizeHint 卡在 minimumHeight，折线只剩一条 9px 的缝
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, data: list[float], upper: float | None = None):
        if upper is not None and upper > 0:
            self._upper = float(upper)
        self._data = data
        self.update()

    def set_color(self, color: str):
        self._color = QColor(color)
        self.update()

    def set_upper(self, upper: float):
        self._upper = float(upper) if upper > 0 else 1.0

    def set_fill_alpha(self, top: int):
        """填充浓度。深色卡上同样的 alpha 会显得厚重得多，要调低。"""
        self._fill_alpha = max(0, min(255, top))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            p.end()
            return

        # 中位参考线：给曲线一个尺度感，否则高低全靠猜
        if self._grid:
            g = QColor(self._color)
            g.setAlpha(38)
            p.setPen(QPen(g, 1, Qt.DashLine))
            p.drawLine(0, int(h * 0.5), w, int(h * 0.5))

        pts = self._data
        if len(pts) < 2:
            pen = QPen(self._color, self._width)
            pen.setCosmetic(True)
            p.setPen(pen)
            p.drawLine(0, int(h / 2), w, int(h / 2))
            p.end()
            return

        window = pts[-120:]
        m = len(window)
        span_x = w - 1
        usable = max(h - 2, 1)
        poly = QPolygonF()
        for i, v in enumerate(window):
            frac = min(max(v / self._upper, 0.0), 1.0)
            poly.append(QPointF(span_x * i / (m - 1), h - frac * usable - 1))

        if self._fill:
            area = QPolygonF(poly)
            area.append(QPointF(span_x, h))
            area.append(QPointF(0.0, h))
            top = QColor(self._color)
            top.setAlpha(self._fill_alpha)
            bottom = QColor(self._color)
            bottom.setAlpha(8)
            grad = QLinearGradient(0, 0, 0, h)
            grad.setColorAt(0.0, top)
            grad.setColorAt(1.0, bottom)
            p.setPen(Qt.NoPen)
            p.setBrush(grad)
            p.drawPolygon(area)

        pen = QPen(self._color, self._width)
        pen.setCosmetic(True)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawPolyline(poly)

        if self._dot:
            # 末端点标"现在"，比让曲线自己停在那儿更清楚
            p.setPen(Qt.NoPen)
            p.setBrush(self._color)
            p.drawEllipse(poly.last(), 2.4, 2.4)
        p.end()


class MeterBar(QWidget):
    """圆角进度条：单段（磁盘）或多段（内存 已用 / 可用）。

    分段时先按整条圆角路径裁剪再填直角色块——每段各自画圆角会在接缝处断开。
    """

    def __init__(self, height: int = 6, parent=None):
        super().__init__(parent)
        self._parts: list[tuple[float, str]] = []
        self._track = QColor(128, 128, 128, 60)
        self.setFixedHeight(height)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_track(self, color: str):
        self._track = QColor(color)
        self.update()

    def set_parts(self, parts):
        self._parts = parts
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        r = QRectF(self.rect())
        radius = r.height() / 2.0
        path = QPainterPath()
        path.addRoundedRect(r, radius, radius)
        p.setClipPath(path)
        p.fillRect(r, self._track)
        x = 0.0
        for frac, color in self._parts:
            w = r.width() * max(0.0, min(1.0, float(frac)))
            if w <= 0:
                continue
            p.fillRect(QRectF(x, 0, w, r.height()), QColor(color))
            x += w
        p.end()


class CoreGrid(QWidget):
    """每核心占用热力格：一格一核，颜色由上层按占用算好传入。"""

    def __init__(self, rows: int = 2, parent=None):
        super().__init__(parent)
        self._rows = max(1, rows)
        self._colors: list[str] = []
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.set_rows(self._rows)

    def set_rows(self, rows: int):
        """核数多时换成两行，格子保持接近正方形而不是被拉成长条。"""
        rows = max(1, rows)
        if rows == self._rows and self.height() == rows * 22 + (rows - 1) * 2:
            return
        self._rows = rows
        self.setFixedHeight(rows * 22 + (rows - 1) * 2)
        self.update()

    def set_data(self, colors: list[str]):
        self._colors = colors
        self.update()

    def paintEvent(self, _):
        n = len(self._colors)
        if not n:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setPen(Qt.NoPen)
        gap = 2.0
        cols = math.ceil(n / self._rows)
        cw = (self.width() - gap * (cols - 1)) / cols
        ch = (self.height() - gap * (self._rows - 1)) / self._rows
        for i, color in enumerate(self._colors):
            row, col = divmod(i, cols)
            p.setBrush(QColor(color))
            p.drawRoundedRect(
                QRectF(col * (cw + gap), row * (ch + gap), cw, ch), 2, 2)
        p.end()
