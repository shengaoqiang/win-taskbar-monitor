r"""GPU 占用 / 显存，走 PDH 性能计数器（无需 NVML、不区分显卡品牌）。

非直觉点：
- 所有 Win32 函数必须设 restype，否则 64 位句柄和 HRESULT 被当 c_int 截断，
  PdhGetFormattedCounterArrayW 成功时返回 0x800007D2，按有符号读会变成负数。
- \GPU Engine(*) 每进程每引擎一条实例（本机 392 条），必须按 luid 聚合，
  否则多显卡的占用会叠成 200%。
- 核显没有独立显存，Dedicated Usage 恒为 0，显存要读 Total Committed。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

_PDH_FMT_DOUBLE = 0x00000200
_PDH_MORE_DATA = 0x800007D2

_GB = 1024.0 ** 3


class _FmtVal(ctypes.Structure):
    _fields_ = [("status", wintypes.DWORD), ("value", ctypes.c_double)]


class _FmtItem(ctypes.Structure):
    _fields_ = [("name", wintypes.LPWSTR), ("fmt", _FmtVal)]


class GpuSampler:
    """每秒调用一次 read()，返回 (占用%, 显存已用 GB)；不可用时返回 None。"""

    def __init__(self):
        self._q = ctypes.c_void_p()
        self._eng = ctypes.c_void_p()
        self._mem = ctypes.c_void_p()
        self.ok = self._open()

    def _open(self) -> bool:
        try:
            pdh = ctypes.WinDLL("pdh.dll")
        except OSError:
            return False
        self._pdh = pdh
        pdh.PdhOpenQueryW.argtypes = [wintypes.LPCWSTR, ctypes.c_size_t,
                                      ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR,
                                              ctypes.c_size_t,
                                              ctypes.POINTER(ctypes.c_void_p)]
        pdh.PdhCollectQueryData.argtypes = [ctypes.c_void_p]
        pdh.PdhCollectQueryData.restype = ctypes.c_ulong
        pdh.PdhGetFormattedCounterArrayW.argtypes = [
            ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(_FmtItem)]
        pdh.PdhGetFormattedCounterArrayW.restype = ctypes.c_ulong

        if pdh.PdhOpenQueryW(None, 0, ctypes.byref(self._q)) != 0:
            return False
        ok_e = pdh.PdhAddEnglishCounterW(
            self._q, r"\GPU Engine(*)\Utilization Percentage", 0,
            ctypes.byref(self._eng)) == 0
        ok_m = pdh.PdhAddEnglishCounterW(
            self._q, r"\GPU Adapter Memory(*)\Total Committed", 0,
            ctypes.byref(self._mem)) == 0
        # 首次 collect 只是建基准，GPU 计数器要两次才有值
        pdh.PdhCollectQueryData(self._q)
        return ok_e and ok_m

    def _array(self, counter):
        size = wintypes.DWORD(0)
        cnt = wintypes.DWORD(0)
        rc = self._pdh.PdhGetFormattedCounterArrayW(
            counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(cnt), None)
        if rc != _PDH_MORE_DATA:
            return []
        buf = ctypes.create_string_buffer(size.value)
        items = ctypes.cast(buf, ctypes.POINTER(_FmtItem))
        rc = self._pdh.PdhGetFormattedCounterArrayW(
            counter, _PDH_FMT_DOUBLE, ctypes.byref(size), ctypes.byref(cnt), items)
        if rc != 0:
            return []
        return [(items[i].name, items[i].fmt.value)
                for i in range(cnt.value) if items[i].fmt.status == 0]

    def read(self):
        if not self.ok:
            return None
        if self._pdh.PdhCollectQueryData(self._q) != 0:
            return None

        # 按 luid 聚合 3D 引擎：实例名形如 pid_123_luid_0x00000000_0x0000FFEF_phys_0_eng_0_engtype_3D
        per_luid: dict[str, float] = {}
        for name, val in self._array(self._eng):
            if not name.endswith("engtype_3D"):
                continue
            key = name.split("_eng_")[0]          # 去掉引擎序号，保留 pid_.._luid_..
            key = key.split("pid_")[-1].split("_luid_")[-1]
            per_luid[key] = per_luid.get(key, 0.0) + val
        if not per_luid:
            return None
        luid, pct = max(per_luid.items(), key=lambda kv: kv[1])

        vram = 0.0
        for name, val in self._array(self._mem):
            if luid.lower() in name.lower():
                vram = val / _GB
                break
        return min(pct, 100.0), vram
