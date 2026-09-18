"""索引补建取消必须等待独立进程 future 收尾，不能遗留 SQLite 临时写入。"""

import asyncio
from pathlib import Path

import pytest
from camera_logs.logs import compression


class _Loop:
    """将 executor 提交固定为测试控制的 future，不启动真实子进程。"""

    def __init__(self, future):
        self.future = future

    def run_in_executor(self, _pool, _function, _target):
        return self.future


@pytest.mark.parametrize("failure", [False, True])
async def test_index_build_cancellation_drains_process_future_before_propagating(monkeypatch, failure):
    """重复取消也必须等 future 结算；后台失败不能掩盖原始取消。"""
    future = asyncio.get_running_loop().create_future()
    monkeypatch.setattr(compression, "_pool", object())
    monkeypatch.setattr(compression.asyncio, "get_running_loop", lambda: _Loop(future))
    task = asyncio.create_task(compression.build_search_index(Path("hour.tar.gz")))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    if failure:
        future.set_exception(OSError("synthetic index failure"))
    else:
        future.set_result(True)
    with pytest.raises(asyncio.CancelledError):
        await task
    assert future.done()
