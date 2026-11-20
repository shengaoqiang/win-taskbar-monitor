"""生成 app.ico（打包用图标）。用法：python tools/make_icon.py

多尺寸 ICO 靠手搓容器：Vista 之后 ICO 允许条目直接存 PNG，
比 PIL 少一个依赖。app.ico 是产物，不进仓库。
"""
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QImage, QPainter, QPen

OUT = Path(__file__).resolve().parent.parent / "app.ico"
S = 512

app = QGuiApplication(sys.argv)
img = QImage(S, S, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing, True)

p.setPen(Qt.NoPen)
p.setBrush(QBrush(QColor("#1c1f26")))
p.drawRoundedRect(QRectF(8, 8, S - 16, S - 16), 96, 96)

bars = [(180, "#2b6cb0"), (260, "#0f9d58"), (330, "#0f9d58"), (400, "#f29100")]
for i, (h, col) in enumerate(bars):
    p.setBrush(QBrush(QColor(col)))
    p.drawRoundedRect(QRectF(96 + i * 84, 448 - h, 56, h), 14, 14)

p.setBrush(Qt.NoBrush)
p.setPen(QPen(QColor("#9aa3b2"), 16, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
p.drawPolyline([QPointF(104, 150), QPointF(196, 96), QPointF(288, 132), QPointF(400, 64)])
p.end()

pngs = []
for s in (256, 128, 64, 48, 32, 24, 16):
    buf = QBuffer()
    buf.open(QIODevice.WriteOnly)
    img.scaled(s, s, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(buf, "PNG")
    pngs.append((s, bytes(buf.data())))
    buf.close()

out = bytearray(struct.pack("<HHH", 0, 1, len(pngs)))
offset = 6 + 16 * len(pngs)
for s, data in pngs:
    d = 0 if s >= 256 else s          # 256 在 ICO 里用 0 表示
    out += struct.pack("<BBBBHHII", d, d, 0, 0, 1, 32, len(data), offset)
    offset += len(data)
for _, data in pngs:
    out += data

OUT.write_bytes(out)
print(f"{OUT}  {len(out)} bytes")
