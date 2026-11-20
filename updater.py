"""检查 GitHub 最新 Release，判断是否有新版本。

只读 releases/latest：draft 和 prerelease 都不会返回，
所以正式版别发成 prerelease，否则永远拿不到。
"""
from __future__ import annotations

import json
import re
import urllib.request

API = ("https://api.github.com/repos/shengaoqiang/win-taskbar-monitor"
       "/releases/latest")
TIMEOUT = 5


def _parse(v: str) -> tuple:
    m = re.match(r"v?(\d+(?:\.\d+)*)", str(v).strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def check(current: str):
    """有新版本返回 (版本号, Release 页面)，否则 None。异常一律吞掉。"""
    try:
        req = urllib.request.Request(API, headers={
            "User-Agent": "TaskbarMonitor",
            "Accept": "application/vnd.github+json",
        })
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            data = json.load(r)
        tag = str(data.get("tag_name") or "")
        if _parse(tag) > _parse(current):
            return tag.lstrip("vV"), str(data.get("html_url") or "")
    except Exception:
        return None
    return None
