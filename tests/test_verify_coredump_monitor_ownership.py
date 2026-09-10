"""核心转储监控真实 Mongo 验证器的无设备夹具测试。"""

import asyncio
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_coredump_monitor_ownership as verifier
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository


def test_runtime_fixture_only_enables_coredump_guard(tmp_path):
    """验证器构造的运行时不启动采集协程，但可执行资源租约判断。"""
    async def scenario():
        repo = Repository(
            AsyncMongoMockClient().camera_logs,
            Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path),
        )
        task = verifier.collecting_task("task-a", "resource-a")
        await repo.db.tasks.insert_one(task)
        await repo.db.resources.insert_one(verifier.online_resource("resource-a"))

        runtime = verifier.guard_runtime(repo, task)

        assert runtime.collector is not None
        assert runtime.background is None
        assert await runtime.coredump_guard() is True

    asyncio.run(scenario())
