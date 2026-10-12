"""节点共享采集缓冲预算；读前预留，写完释放，缩容只能等待已有数据排空。"""

import asyncio
import logging
import time
import weakref

READ_BYTES = 4096
# 每个输入字节均为换行时也覆盖上海时间前缀的最坏膨胀，不含协议库内部缓存。
RESERVATION_BYTES = READ_BYTES * 32
logger = logging.getLogger(__name__)
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

    def release(self, size, *, owner="unknown"):
        """先验证再修改预算，优化模式也不允许负释放或双重释放污染全局余额。"""
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= self.used:
            logger.error("采集缓冲结算违规 owner=%s release=%s used=%s", owner, size, self.used)
            raise RuntimeError(f"采集缓冲结算违规 owner={owner}")
        self.used -= size
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


class BufferReservation:
    """把单会话读前预留、净结算和最终释放收拢，余额只由此对象变更。"""

    def __init__(self, owner):
        self.owner, self.held, self.read_pending = owner, 0, False

    async def acquire_read(self):
        """一个接收器最多持有一个尚未结算的读请求，取消等待不会增加余额。"""
        if self.read_pending:
            raise RuntimeError(f"重复预留采集读取 owner={self.owner}")
        await budget().acquire()
        self.held += RESERVATION_BYTES
        self.read_pending = True

    def settle_read(self, size):
        """读失败/EOF传零，成功传加前缀后的净字节；拒绝重复结算和超额正文。"""
        if not self.read_pending or isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= RESERVATION_BYTES:
            logger.error("采集读取结算违规 owner=%s bytes=%s", self.owner, size)
            raise RuntimeError(f"采集读取结算违规 owner={self.owner}")
        returned = RESERVATION_BYTES - size
        budget().release(returned, owner=self.owner)
        self.held -= returned
        self.read_pending = False

    def release_all(self):
        """批次物理写入结束或会话终结后释放本对象余额；重复清理不重复归还。"""
        budget().release(self.held, owner=self.owner)
        self.held, self.read_pending = 0, False
