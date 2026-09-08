"""定时后台错误必须可见并关闭原会话，不能静默丢失后续命令。"""

import asyncio
import logging

import pytest
from camera_logs.collection.collector import Collector
from test_collector import FakeConnection


async def test_failed_result_callback_closes_session_and_logs_identity(tmp_path, caplog):
    """设备只收到一次命令；不可恢复回写错误唤醒会话收尾并记录任务身份。"""
    connection = FakeConnection()

    async def failed_result(*_args):
        raise ValueError("injected invalid execution update")

    collector = Collector({"id": "task", "runId": "run", "storageIdentity": "synthetic",
        "initialCommands": [], "scheduledCommands": [{"id": "periodic", "command": "probe",
            "totalExecutions": 2, "intervalSeconds": .01}]}, tmp_path,
        connection_factory=lambda _: connection, update_execution=failed_result)
    caplog.set_level(logging.ERROR)
    await collector.start()
    try:
        await asyncio.wait_for(collector.wait_closed(), 1)
        assert connection.closed
        assert connection.sent == [b"probe\n"]
        assert "定时命令后台执行失败" in caplog.text
        assert "task=task" in caplog.text
        assert "command=periodic" in caplog.text
        with pytest.raises(ValueError, match="injected invalid"):
            await collector.stop()
    finally:
        await asyncio.gather(collector.stop(), return_exceptions=True)
