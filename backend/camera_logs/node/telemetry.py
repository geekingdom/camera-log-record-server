"""线程安全采样节点资源；内部回环和容器桥接流量不计入节点网络吞吐。"""
import os
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import psutil

_PROC_LOCK = threading.Lock()
_INTERNAL = ("lo", "veth", "br-", "docker", "cni")


class TelemetrySampler:
    """保存前一时刻 CPU 时间和网络计数，首样本及计数回退均保持未知。"""

    def __init__(self, host_proc_root: Path | None = None):
        self.host_proc_root, self.previous = host_proc_root, None

    def sample(self):
        """读取单次一致快照；host proc 切换受全局锁保护并立即恢复。"""
        host = self.host_proc_root is not None and (self.host_proc_root / "1/net/dev").is_file()
        scope = "HOST" if host or (os.name != "posix" or not Path("/.dockerenv").exists()) else "RUNTIME"
        with _PROC_LOCK:
            original = getattr(psutil, "PROCFS_PATH", None)
            try:
                if host and original is not None:
                    psutil.PROCFS_PATH = str(self.host_proc_root)
                cpu, memory = psutil.cpu_times(), psutil.virtual_memory()
            finally:
                if original is not None:
                    psutil.PROCFS_PATH = original
        moment, counters = time.monotonic(), self._network(self.host_proc_root / "1/net/dev" if host else None)
        cpu_percent = upload = download = None
        if self.previous:
            old_moment, old_cpu, old_net = self.previous
            # guest/guest_nice 已包含在 user/nice，重复计入会低估真实使用率。
            total = self._cpu_total(cpu) - self._cpu_total(old_cpu)
            idle = (cpu.idle + getattr(cpu, "iowait", 0)) - (
                old_cpu.idle + getattr(old_cpu, "iowait", 0)
            )
            if total > 0:
                cpu_percent = max(0, min(100, (total - idle) * 100 / total))
            elapsed = moment - old_moment
            if elapsed > 0 and counters.keys() == old_net.keys():
                deltas = [
                    (counters[name][0] - old_net[name][0], counters[name][1] - old_net[name][1])
                    for name in counters
                ]
                if all(download_delta >= 0 and upload_delta >= 0 for download_delta, upload_delta in deltas):
                    download = sum(delta[0] for delta in deltas) / elapsed
                    upload = sum(delta[1] for delta in deltas) / elapsed
        self.previous = moment, cpu, counters
        return {
            "sampledAt": datetime.now(UTC),
            "scope": scope,
            "status": "OK",
            "cpuPercent": cpu_percent,
            "memoryUsedBytes": memory.used,
            "memoryTotalBytes": memory.total,
            "memoryPercent": memory.percent,
            "networkUploadBytesPerSecond": upload,
            "networkDownloadBytesPerSecond": download,
        }

    @staticmethod
    def _cpu_total(cpu):
        """排除 Linux 已并入用户态的 guest 计数，保留其它 CPU 时间字段。"""
        fields = getattr(cpu, "_fields", ())
        if fields:
            return sum(value for name, value in zip(fields, cpu) if name not in {"guest", "guest_nice"})
        return sum(cpu)

    def _network(self, proc_path):
        """返回每个外部网卡的累计接收、发送字节；接口集合变化视为未知样本。"""
        if proc_path:
            rows = [
                (line.split(":", 1)[0].strip(), line.split(":", 1)[1].split())
                for line in proc_path.read_text().splitlines()[2:]
            ]
            return {
                name: (int(row[0]), int(row[8]))
                for name, row in rows
                if not name.startswith(_INTERNAL)
            }
        values = psutil.net_io_counters(pernic=True)
        return {
            name: (item.bytes_recv, item.bytes_sent)
            for name, item in values.items()
            if not name.startswith(_INTERNAL)
        }
