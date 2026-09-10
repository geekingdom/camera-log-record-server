"""Worker 失败隔离测试：单路停止异常不能阻止其他设备连接关闭。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from camera_logs.node.worker import Worker


@pytest.mark.asyncio
async def test_database_outage_stops_all_sessions_and_keeps_only_unconfirmed_instance():
    failing = SimpleNamespace(stop=AsyncMock(side_effect=OSError("flush failed")))
    stopped = SimpleNamespace(stop=AsyncMock())
    worker = Worker(SimpleNamespace(settings=SimpleNamespace(host_proc_root=None)))
    worker.active = {"failed": failing, "stopped": stopped}

    await worker.isolate_active_sessions()

    failing.stop.assert_awaited_once()
    stopped.stop.assert_awaited_once()
    assert set(worker.active) == {"failed"}
