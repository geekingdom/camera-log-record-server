"""开发数据重置必须保留配置集合，并在活动任务或运行存在时拒绝执行。"""

import runpy
from pathlib import Path

import pytest
from mongomock_motor import AsyncMongoMockClient

_script = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/reset_development_data.py"))
RESET_COLLECTIONS = _script["RESET_COLLECTIONS"]
reset_development_data = _script["reset_development_data"]


@pytest.mark.asyncio
async def test_reset_clears_task_history_resources_and_log_root_but_preserves_configuration(tmp_path):
    """受控重置删除任务历史与资源，保留令牌、节点和命令模板配置。"""
    database = AsyncMongoMockClient().db
    for collection in RESET_COLLECTIONS:
        document = {"id": collection}
        if collection == "tasks":
            document.update(nodeId=None, desiredState="STOPPED", status="BLOCKED")
        if collection == "runs":
            document["endedAt"] = "done"
        await database[collection].insert_one(document)
    for collection in ("tokens", "nodes", "node_configs", "platform_settings", "templates"):
        await database[collection].insert_one({"id": collection})
    (tmp_path / "task-log").mkdir()
    (tmp_path / "task-log" / "entry.log").write_text("log")
    (tmp_path / "fragment.tmp").write_text("tmp")

    result = await reset_development_data(database, tmp_path)

    assert result["database"] == "db"
    assert result["logEntries"] == 2
    for collection in RESET_COLLECTIONS:
        assert await database[collection].count_documents({}) == 0
    for collection in ("tokens", "nodes", "node_configs", "platform_settings", "templates"):
        assert await database[collection].count_documents({}) == 1
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_reset_refuses_owned_running_or_active_run_without_touching_data(tmp_path):
    """运行中任务、节点归属或未结束运行必须阻断删除和日志目录清理。"""
    database = AsyncMongoMockClient().db
    await database.tasks.insert_one({"id": "running", "nodeId": None, "status": "RUNNING", "desiredState": "RUNNING"})
    await database.commands.insert_one({"id": "command"})
    (tmp_path / "must-stay").write_text("log")

    with pytest.raises(RuntimeError, match="存在活动任务"):
        await reset_development_data(database, tmp_path)
    assert await database.commands.count_documents({}) == 1
    assert (tmp_path / "must-stay").exists()

    await database.tasks.delete_many({})
    await database.runs.insert_one({"id": "active-run"})
    with pytest.raises(RuntimeError, match="存在活动运行"):
        await reset_development_data(database, tmp_path)
    assert await database.commands.count_documents({}) == 1
