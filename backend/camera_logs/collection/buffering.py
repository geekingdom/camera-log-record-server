"""节点共享采集缓冲预算；读前预留，写完释放，缩容只能等待已有数据排空。"""

import asyncio
import time
import weakref

READ_BYTES = 4096
# 每个输入字节均为换行时也覆盖上海时间前缀的最坏膨胀，不含协议库内部缓存。
RESERVATION_BYTES = READ_BYTES * 32
_states = weakref.WeakKeyDictionary()
_configuration = {"bufferMiB": 64, "batchKiB": 256, "flushIntervalMs": 100}


class BufferBudget:
    """事件循环内共享硬字节预算，队列不存正文且取消不遗留预留。"""

    def __init__(self):
        self.used = 0
        self.pending = {}
        self._changed = asyncio.Event()

    def available(self):
        """预算缩小低于已用时不撤销旧数据，只阻止后续接收。"""
        return self.used + RESERVATION_BYTES <= _configuration["bufferMiB"] * 1024 * 1024

    async def acquire(self):
        """等待释放或配置变化；取得预留后由调用者负责释放。"""
        while not self.available():
            observed = self._changed
            await observed.wait()
            if observed is self._changed:
                observed.clear()
        self.used += RESERVATION_BYTES

    def release(self, size):
        """只释放本会话持有的预算，禁止用丢弃正文换取内存。"""
        self.used -= size
        assert self.used >= 0
        self._changed.set()

    def notify_configuration_changed(self):
        """扩容或缩容后唤醒等待者重新计算容量，不撤销既有预留。"""
        self._changed.set()


def budget():
    """每事件循环独立，避免测试或多应用循环共享异步状态。"""
    loop = asyncio.get_running_loop()
    if loop not in _states:
        _states[loop] = BufferBudget()
    return _states[loop]


def configure(effective):
    """从已校验的节点有效参数热更新，已有预留不会被强行回收。"""
    for name in _configuration:
        _configuration[name] = effective[name]
    for state in tuple(_states.values()):
        state.notify_configuration_changed()


def batch_parameters():
    """批量按字节/最长等待双门槛，低内存预算下仍给其他会话留出空间。"""
    return min(_configuration["batchKiB"] * 1024, _configuration["bufferMiB"] * 1024 * 1024 // 8), _configuration["flushIntervalMs"] / 1000


def snapshot():
    """暴露应用预留字节及最老待写年龄，明确不含协议库和操作系统缓存。"""
    state = budget()
    return {"reservedBytes": state.used, "limitBytes": _configuration["bufferMiB"] * 1024 * 1024,
            "oldestPendingMs": max(0, time.monotonic() - min(state.pending.values())) * 1000 if state.pending else 0,
            "scope": "APPLICATION_BATCH_RESERVATIONS"}
