"""资源数据采集层：psutil 取数 + 速率差分 + 历史环形缓冲。

设计说明（非直觉点）：
- psutil.cpu_percent(interval=None) 必须周期性调用两次才有值，第一次返回 0.0，
  所以采集循环里要"先调用拿到上次基准"，这里用一次调用即可，因为定时器反复调用。
- 网络/磁盘只有累计计数 net_io_counters()/disk_io_counters()，速率=两次差分/时间差。
- 历史用 deque(maxlen) 环形缓冲，画图直接消费。
"""
from __future__ import annotations

import os
import time
import threading
from collections import deque

import psutil

from gpu import GpuSampler

HIST_LEN = 120          # 历史点数（每秒一点 => 120 秒）
TOP_PROC = 6            # 面板显示 Top N 进程


class Sample:
    """单帧完整指标快照。"""
    __slots__ = (
        "ts", "cpu", "cpu_cores", "mem_pct", "mem_used_gb", "mem_total_gb",
        "mem_avail_gb",
        "net_up", "net_down", "net_nics", "disk_read", "disk_write",
        "disk_used_pct", "disk_used_gb", "disk_total_gb", "disk_drive",
        "partitions", "gpu_pct", "vram_gb", "uptime",
        "top_procs",
    )

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


class Collector:
    """后台采集器。线程安全：外部持锁读 last / history。"""

    def __init__(self, proc_refresh: float = 5.0):
        self._lock = threading.Lock()
        self._last = Sample(ts=time.time())
        self.history: deque = deque(maxlen=HIST_LEN)

        # 差分基准
        self._net_prev = psutil.net_io_counters()
        self._nic_prev = psutil.net_io_counters(pernic=True)
        self._disk_prev = psutil.disk_io_counters()
        self._t_prev = time.time()
        self._boot_ts = psutil.boot_time()
        self._proc_refresh = proc_refresh
        self._top_procs = []
        # 系统盘：不写死 C:，改装系统在 D: 的机器也能读到正确容量
        self._sys_drive = (os.environ.get("SystemDrive") or "C:") + "\\"
        # GPU 走 PDH 计数器（见 gpu.py）；取不到就把两列显示成 --
        self._gpu = GpuSampler()

        # 预热 cpu_percent 基准
        self._last_cpu = psutil.cpu_percent(interval=None)

        # 进程扫描独立线程：见 _scan_loop 说明。
        # 面板已不显示进程列表，默认不启动——它每 5 秒要烧掉约 650ms CPU。
        # 需要恢复时把下面两行取消注释即可。
        self._proc_thread = None
        # self._proc_thread = threading.Thread(
        #     target=self._scan_loop, name="proc-scan", daemon=True)
        # self._proc_thread.start()

    def tick(self) -> Sample:
        """被定时器周期性调用，刷新 last 与 history。"""
        now = time.time()

        cpu = psutil.cpu_percent(interval=None)
        cores = psutil.cpu_percent(interval=None, percpu=True)
        mem = psutil.virtual_memory()
        du = psutil.disk_usage(self._sys_drive)
        dt = max(now - self._t_prev, 1e-6)

        # 网络速率
        net = psutil.net_io_counters()
        net_up = (net.bytes_sent - self._net_prev.bytes_sent) / dt
        net_down = (net.bytes_recv - self._net_prev.bytes_recv) / dt
        self._net_prev = net

        # 分网卡速率：虚拟网卡/Loopback 恒为 0，只留真正在跑的，按流量降序取前 3
        nics = []
        cur_nic = psutil.net_io_counters(pernic=True)
        for name, c in cur_nic.items():
            prev = self._nic_prev.get(name, c)
            up = (c.bytes_sent - prev.bytes_sent) / dt
            down = (c.bytes_recv - prev.bytes_recv) / dt
            if down + up > 1:
                nics.append((name, max(up, 0.0), max(down, 0.0)))
        self._nic_prev = cur_nic
        nics.sort(key=lambda x: x[1] + x[2], reverse=True)
        disk_read = disk_write = 0.0
        dio = psutil.disk_io_counters()
        if dio:
            disk_read = (dio.read_bytes - self._disk_prev.read_bytes) / dt
            disk_write = (dio.write_bytes - self._disk_prev.write_bytes) / dt
            self._disk_prev = dio
        self._t_prev = now

        g = self._gpu.read()
        gpu_pct, vram_gb = (g if g else (None, None))

        s = Sample(
            ts=now,
            cpu=cpu,
            cpu_cores=cores,
            mem_pct=mem.percent,
            mem_used_gb=mem.used / 1024 ** 3,
            mem_total_gb=mem.total / 1024 ** 3,
            mem_avail_gb=mem.available / 1024 ** 3,
            net_up=net_up,
            net_down=net_down,
            net_nics=nics[:3],
            disk_read=disk_read,
            disk_write=disk_write,
            disk_used_pct=du.percent,
            disk_used_gb=du.used / 1024 ** 3,
            disk_total_gb=du.total / 1024 ** 3,
            disk_drive=self._sys_drive[0],
            partitions=self._partitions(),
            gpu_pct=gpu_pct,
            vram_gb=vram_gb,
            uptime=now - self._boot_ts,
            top_procs=None,
        )

        with self._lock:
            s.top_procs = self._top_procs
            self._last = s
            self.history.append(s)
        return s

    def _partitions(self):
        """各分区占用：[(挂载点名, 已用GB, 总量GB, 百分比)]。

        光驱/未就绪的盘 disk_usage 会抛 OSError，跳过而不是让整帧失败。
        """
        out = []
        for p in psutil.disk_partitions(all=False):
            try:
                du = psutil.disk_usage(p.mountpoint)
            except OSError:
                continue
            name = p.mountpoint.rstrip("\\") or p.mountpoint
            out.append((name, du.used / 1024 ** 3, du.total / 1024 ** 3, du.percent))
        return out

    def _scan_loop(self):
        """后台线程周期性扫描 Top 进程。

        memory_percent 每进程一次 GetProcessMemoryInfo（336 进程实测约 650ms），
        留在 UI 线程的 tick 里会每 5 秒僵一次界面，所以独立成线程。
        """
        while True:
            procs = self._scan_procs()
            if procs is not None:
                with self._lock:
                    self._top_procs = procs
            time.sleep(self._proc_refresh)

    @staticmethod
    def _scan_procs():
        try:
            procs = []
            for p in psutil.process_iter(["pid", "name", "memory_percent", "cpu_percent"]):
                try:
                    info = p.info
                    procs.append((info.get("name") or "unknown",
                                  info.get("memory_percent") or 0.0,
                                  info.get("pid")))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            procs.sort(key=lambda x: x[1], reverse=True)
            return procs[:TOP_PROC]
        except Exception:
            return None

    @property
    def last(self) -> Sample:
        with self._lock:
            return self._last

    def get_history(self):
        with self._lock:
            return list(self.history)

    # ---- 格式化助手 ----
    @staticmethod
    def fmt_bytes(n: float) -> str:
        if n >= 1024 ** 3:
            return f"{n/1024**3:.2f} GB"
        if n >= 1024 ** 2:
            return f"{n/1024**2:.1f} MB"
        if n >= 1024:
            return f"{n/1024:.1f} KB"
        return f"{n:.0f} B"

    @classmethod
    def fmt_speed(cls, bps: float) -> str:
        return cls.fmt_bytes(bps) + "/s"

    @classmethod
    def fmt_speed_compact(cls, bps: float) -> str:
        """任务栏条专用紧凑速率：11.9M / 124M / 1.2G。

        条上网速组可用宽度只有约 98px，"11.9 MB/s"（61px）会截断，所以去掉
        空格和 "/s"；超过 100 再省掉小数位，最坏情况也不会超过 5 个字符。
        """
        if bps >= 1024 ** 3:
            return f"{bps / 1024 ** 3:.1f}G"
        if bps >= 1024 ** 2:
            mb = bps / 1024 ** 2
            return f"{mb:.1f}M" if mb < 99.95 else f"{mb:.0f}M"
        if bps >= 1024:
            kb = bps / 1024
            return f"{kb:.1f}K" if kb < 99.95 else f"{kb:.0f}K"
        return f"{bps:.0f}B"
