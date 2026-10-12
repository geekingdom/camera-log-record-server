"""节点字节预算与热配置必须保序背压，不能以丢弃正文释放容量。"""

import asyncio

import pytest
from camera_logs.collection import buffering
from camera_logs.collection.collector import Collector
from camera_logs.logs.archive_access import ReadLimiter


@pytest.fixture(autouse=True)
def defaults():
    """测试不能把动态配置留给后续独立采集用例。"""
    previous = dict(buffering._configuration)
    yield
    buffering.configure(previous)


def _reservation_count(mebibytes):
    """按当前读前最坏膨胀预留计算可同时接收的会话数。"""
    return mebibytes * 1024 * 1024 // buffering.RESERVATION_BYTES


async def test_budget_release_wakes_waiter_without_polling():
    """写盘释放预留后等待会话立刻重算预算，不依赖定时轮询。"""
    buffering.configure({"bufferMiB": 16, "batchKiB": 256, "flushIntervalMs": 100})
    state = buffering.budget()
    for _ in range(_reservation_count(16)):
        await state.acquire()
    waiting = asyncio.create_task(state.acquire())
    await asyncio.sleep(0)
    assert not waiting.done()
    state.release(buffering.RESERVATION_BYTES)
    await asyncio.wait_for(waiting, .2)
    assert state.used == 16 * 1024 * 1024
    state.release(state.used)
    assert state.used == 0


async def test_budget_expansion_wakes_waiter_without_releasing_existing_data():
    """扩容配置必须唤醒等待者，既有预留不需先被丢弃或提前释放。"""
    buffering.configure({"bufferMiB": 16, "batchKiB": 256, "flushIntervalMs": 100})
    state = buffering.budget()
    for _ in range(_reservation_count(16)):
        await state.acquire()
    waiting = asyncio.create_task(state.acquire())
    await asyncio.sleep(0)
    buffering.configure({"bufferMiB": 32, "batchKiB": 256, "flushIntervalMs": 100})
    await asyncio.wait_for(waiting, .2)
    assert state.used == 16 * 1024 * 1024 + buffering.RESERVATION_BYTES
    state.release(state.used)


async def test_budget_cancelled_waiter_never_leaks_a_reservation():
    """等待配置或释放期间取消不能占用尚未取得的预算。"""
    buffering.configure({"bufferMiB": 16, "batchKiB": 256, "flushIntervalMs": 100})
    state = buffering.budget()
    for _ in range(_reservation_count(16)):
        await state.acquire()
    waiting = asyncio.create_task(state.acquire())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert state.used == 16 * 1024 * 1024
    state.release(state.used)


async def test_budget_shrink_waits_for_existing_data_to_drain():
    """缩容后保留既有预留，新读取只能等占用降回新的容量上限。"""
    buffering.configure({"bufferMiB": 32, "batchKiB": 256, "flushIntervalMs": 100})
    state = buffering.budget()
    for _ in range(_reservation_count(16) + 1):
        await state.acquire()
    buffering.configure({"bufferMiB": 16, "batchKiB": 256, "flushIntervalMs": 100})
    waiting = asyncio.create_task(state.acquire())
    await asyncio.sleep(0)
    assert not waiting.done()
    state.release(state.used)
    await asyncio.wait_for(waiting, .2)
    assert state.used == buffering.RESERVATION_BYTES
    state.release(buffering.RESERVATION_BYTES)


async def test_read_timeout_releases_and_does_not_reset_device_idle(tmp_path):
    requested = []

    class Connection:
        async def read(self, size):
            requested.append(size)
            await asyncio.Event().wait()

    collector = Collector({"id": "budget", "storageIdentity": "test"}, tmp_path, connection_factory=lambda _: None)
    collector._connection = Connection()
    original = collector.liveness_read_observation_started
    with pytest.raises(TimeoutError):
        await collector._read_budgeted(.01)
    assert buffering.snapshot()["reservedBytes"] == 0
    assert collector.liveness_read_observation_started == original
    assert requested == [buffering.READ_BYTES]


def test_rate_update_preserves_existing_read_debt():
    limiter = ReadLimiter(1000)
    limiter.next_at = 99
    limiter.configure(2000)
    assert limiter.rate == 2000 and limiter.next_at == 99
    with pytest.raises(ValueError):
        limiter.configure(0)


def test_batch_size_and_interval_are_hot_configurable():
    buffering.configure({"bufferMiB": 16, "batchKiB": 512, "flushIntervalMs": 25})
    assert buffering.batch_parameters() == (512 * 1024, .025)


async def test_invalid_budget_release_never_mutates_balance(caplog):
    """负数/超额释放先拒绝，不能先减成负数再依赖可剥离断言。"""
    state = buffering.budget()
    await state.acquire()
    for size in (-1, state.used + 1, True):
        with pytest.raises(RuntimeError, match="session-test"):
            state.release(size, owner="session-test")
        assert state.used == buffering.RESERVATION_BYTES
    assert "session-test" in caplog.text
    state.release(state.used)


async def test_owned_reservations_settle_once_and_cleanup_is_idempotent():
    """一个会话不能通过重复结算释放另一会话余额，批次与读异常共享净结算。"""
    first = buffering.BufferReservation("first")
    second = buffering.BufferReservation("second")
    await first.acquire_read()
    await second.acquire_read()
    first.settle_read(13)
    with pytest.raises(RuntimeError):
        first.settle_read(0)
    assert buffering.budget().used == buffering.RESERVATION_BYTES + 13
    first.release_all()
    first.release_all()
    assert buffering.budget().used == buffering.RESERVATION_BYTES
    second.settle_read(0)
    second.release_all()
    assert buffering.budget().used == 0
