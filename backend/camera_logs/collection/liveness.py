"""采集会话的本机接收存活证据。

该模块不持久化状态、不访问设备或数据库；它只根据单调时钟和 Collector 的即时状态，
让 Worker 判断何时可请求健康策略停止一个已认证失败的设备任务。
"""

from __future__ import annotations

import time


class RuntimeLiveness:
    """保留同一运行跨重连的最后接收时间及断连宽限。"""

    def __init__(self, started_monotonic=None):
        self.started_monotonic = time.monotonic() if started_monotonic is None else started_monotonic
        self.last_received_monotonic = None
        self.observation_reset_monotonic = None
        self.disconnected_monotonic = None

    def received(self, collector):
        """在读协程取得原始字节后立刻登记，早于前缀、写盘和实时发布。"""
        if collector.last_received_monotonic is not None:
            self.last_received_monotonic = collector.last_received_monotonic
            self.disconnected_monotonic = None

    def observation_reset(self, instant):
        """存储背压结束后重开十秒观察，期间不能证明设备没有继续输出。"""
        self.observation_reset_monotonic = instant

    def state_changed(self, state, collector, current_monotonic):
        """仅当前会话的终结状态开启断连宽限，旧会话回调不能影响后继。"""
        if state == "COLLECTING":
            self.disconnected_monotonic = None
        elif state in {"IDLE_TIMEOUT", "READ_ERROR", "CLOSED"}:
            self.disconnected_monotonic = self.disconnected_monotonic or current_monotonic

    def allows_health_stop(self, task, collector, *, stopping, retired, current_monotonic):
        """确认十秒无字节和无可用连接；断连可在无输出后额外保留十秒宽限。"""
        protocol = task.get("protocol") or task.get("protocolType")
        if protocol not in {"SSH", "TELNET_DEVICE"} or stopping or retired:
            return False
        timeout = float(task.get("logIdleTimeoutSeconds", 10))
        if collector is None:
            return current_monotonic - self.started_monotonic >= timeout
        if getattr(collector, "liveness_storage_busy", False):
            return False
        if getattr(collector, "liveness_waiting_for_read", False):
            evidence = max(
                self.last_received_monotonic or self.started_monotonic,
                self.observation_reset_monotonic or self.started_monotonic,
            )
            return current_monotonic - evidence >= timeout
        if self.disconnected_monotonic is not None:
            return current_monotonic - self.disconnected_monotonic >= timeout
        return False
