"""开机自启：写 HKCU 的 Run 键（不碰 HKLM，不需要管理员）。

注册表里存的就是完整命令行，读回来跟当前命令行比对——
程序被挪过目录时算"未启用"，用户重开一次就会写成新路径。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "TaskbarMonitor"
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _command() -> str:
    # 打包版 exe 自己就是入口，sys.executable 即目标程序
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    exe = Path(sys.executable)
    # 用 python.exe 启动开机时会闪一下黑框，统一换成 pythonw.exe
    if exe.name.lower() == "python.exe":
        quiet = exe.with_name("pythonw.exe")
        if quiet.exists():
            exe = quiet
    script = Path(__file__).resolve().with_name("main.py")
    return f'"{exe}" "{script}"'


def is_enabled() -> bool:
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            val, _ = winreg.QueryValueEx(k, APP_NAME)
    except OSError:
        return False
    return str(val).strip().lower() == _command().lower()


def set_enabled(on: bool) -> bool:
    """返回是否写成功，调用方据此回滚菜单勾选。"""
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, _command())
            else:
                try:
                    winreg.DeleteValue(k, APP_NAME)
                except FileNotFoundError:
                    pass        # 本来就没写，等同于已关闭
    except OSError:
        return False
    return True
