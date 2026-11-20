# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置。用法：pyinstaller --noconfirm TaskbarMonitor.spec

剪掉 Quick/Qml/Pdf 与软件 OpenGL：界面全是 QPainter 自绘，走 raster 渲染，
用不到这些，不剪会多出 35MB。
"""

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickWidgets",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtMultimedia", "PySide6.QtTest", "PySide6.QtSql",
        "PySide6.QtPdf", "PySide6.QtDesigner", "PySide6.QtCharts",
    ],
    noarchive=False,
)

_DROP = ("Qt6Quick", "Qt6Qml", "Qt6Pdf", "opengl32sw")
a.binaries = [b for b in a.binaries if not any(k in b[0] for k in _DROP)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="TaskbarMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon="app.ico",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="TaskbarMonitor",
)
