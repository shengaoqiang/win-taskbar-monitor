"""配置持久化：JSON 存盘。读坏了/字段类型不对一律回默认，永不抛异常。

放在程序目录旁（config.json），便携、可直接手改。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _base_dir() -> Path:
    """打包成 exe 后 __file__ 指向临时解包目录，配置必须落在 exe 旁边，
    否则每次退出都丢。exe 目录不可写（装了 Program Files）就退到 APPDATA。"""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    d = Path(sys.executable).resolve().parent
    if os.access(d, os.W_OK):
        return d
    d = Path(os.environ.get("APPDATA") or Path.home()) / "TaskbarMonitor"
    d.mkdir(parents=True, exist_ok=True)
    return d


CFG_PATH = _base_dir() / "config.json"

DEFAULTS = {
    "theme_mode": "follow",   # follow / dark / light
    "strip_visible": True,
    "strip_pos": None,        # [x, y]；None = 贴任务栏最左
    "panel_pos": None,
}


def _point(v):
    """把 JSON 里的坐标规整成 [x, y]；格式不对返回 None。"""
    if (isinstance(v, (list, tuple)) and len(v) == 2
            and all(isinstance(i, int) and not isinstance(i, bool) for i in v)):
        return [v[0], v[1]]
    return None


def load() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return cfg
    if not isinstance(data, dict):
        return cfg
    if data.get("theme_mode") in ("follow", "dark", "light"):
        cfg["theme_mode"] = data["theme_mode"]
    if isinstance(data.get("strip_visible"), bool):
        cfg["strip_visible"] = data["strip_visible"]
    for k in ("strip_pos", "panel_pos"):
        p = _point(data.get(k))
        if p:
            cfg[k] = p
    return cfg


def save(cfg: dict) -> None:
    """原子写：先落 .tmp 再 replace，写一半断掉也不会留坏文件。"""
    try:
        tmp = str(CFG_PATH) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CFG_PATH)
    except Exception:
        pass
