"""验证手动命令准入在事务回调内重新确认采集会话与队列额度。"""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


class FakeTasks:
    """记录原子领取条件的内存任务集合，不模拟 MongoDB 事务回滚。"""

    def __init__(self, document, actions):
        self.document = document
        self.actions = actions
        self.queries = []

    async def find_one_and_update(self, query, update, **kwargs):
        """仅当所有 owner、会话和交互状态条件仍满足时增加领取版本。"""
        self.actions.append("update")
        self.queries.append((query, update, kwargs))
        current = self.document
        if any(current.get(key) != value for key, value in query.items() if not isinstance(value, dict)):
            return None
        if current.get("resourceDeleted") is True:
            return None
        current["commandClaimVersion"] = current.get("commandClaimVersion", 0) + update["$inc"]["commandClaimVersion"]
        return deepcopy(current)


class FakeCommands:
    """只实现准入所需的计数和插入，以检查同一运行会话的查询范围。"""

    def __init__(self, documents, actions):
        self.documents = documents
        self.actions = actions
        self.count_queries = []

    async def count_documents(self, query, **kwargs):
        """按准入查询的全部字段计数，防止测试替身掩盖会话过滤遗漏。"""
        self.actions.append("count")
        self.count_queries.append((query, kwargs))
        return sum(all(document.get(key) == value for key, value in query.items()) for document in self.documents)

    async def insert_one(self, document, **kwargs):
        """保存插入参数，供成功路径核对固定命令 ID 和会话绑定字段。"""
        self.actions.append("insert")
        self.documents.append(deepcopy(document))


def task_snapshot():
    """构造当前节点持有且可交互的采集任务快照。"""
    return {"id": "task", "runId": "run", "nodeId": "node", "generation": 3, "sessionId": "session",
            "status": "COLLECTING", "desiredState": "RUNNING", "resourceDeleted": False,
            "commandClaimVersion": 7}


def command_body():
    """模拟已由 API 模型校验后的手动命令配置字典。"""
    return {"command": "ls", "newline": "\n", "delaySeconds": 0, "prompt": None, "timeoutSeconds": 30}


def command(task, *, session_id=None, index=0):
    """构造已排队命令，可指定不同会话以验证配额隔离。"""
    return {"id": f"existing-{index}", "taskId": task["id"], "runId": task["runId"],
            "sessionId": session_id or task["sessionId"], "kind": "MANUAL", "status": "QUEUED"}


def repository(current, commands):
    """将内存集合组合为准入函数需要的 repo.db 形状。"""
    actions = []
    tasks = FakeTasks(current, actions)
    command_collection = FakeCommands(commands, actions)
    return SimpleNamespace(db=SimpleNamespace(tasks=tasks, commands=command_collection)), tasks, command_collection, actions


@pytest.fixture
def admit(monkeypatch):
    """空会话只执行事务回调；真实 Mongo 提交或回滚不由该单元测试声称覆盖。"""
    from camera_logs.commands import reservation
    from camera_logs.commands.manual_admission import admit_manual

    async def transaction_stub(_repo, callback):
        return await callback(None)

    monkeypatch.setattr(reservation, "reservation_transaction", transaction_stub)
    return admit_manual


async def test_manual_admission_rejects_current_session_queue_at_capacity(admit):
    """当前 run/session 已有 100 条待发送命令时必须在事务内拒绝新命令。"""
    task = task_snapshot()
    repo, tasks, commands, actions = repository(deepcopy(task), [command(task, index=index) for index in range(100)])

    with pytest.raises(HTTPException) as rejected:
        await admit(repo, task, "fixed-command", command_body(), "operator")

    assert rejected.value.status_code == 429
    assert actions == ["update", "count"]
    query, update, kwargs = tasks.queries[0]
    assert query == {"id": "task", "runId": "run", "nodeId": "node", "generation": 3,
                     "sessionId": "session", "status": "COLLECTING", "desiredState": "RUNNING",
                     "resourceDeleted": {"$ne": True}}
    assert update == {"$inc": {"commandClaimVersion": 1}}
    assert kwargs["session"] is None
    assert commands.documents == [command(task, index=index) for index in range(100)]


async def test_manual_admission_ignores_queued_commands_from_other_sessions(admit):
    """旧会话积压达到上限不能占用当前会话的手动命令配额。"""
    task = task_snapshot()
    repo, _, commands, actions = repository(deepcopy(task), [
        command(task, session_id="old-session", index=index) for index in range(100)
    ])

    result = await admit(repo, task, "fixed-command", command_body(), "operator")

    assert actions == ["update", "count", "insert"]
    assert commands.count_queries[0][0] == {"taskId": "task", "runId": "run", "sessionId": "session",
                                              "kind": "MANUAL", "status": "QUEUED"}
    assert result["id"] == "fixed-command"


@pytest.mark.parametrize("change", [
    {"generation": 4},
    {"sessionId": "new-session"},
    {"status": "PAUSED"},
])
async def test_manual_admission_rejects_stale_owner_session_or_state(admit, change):
    """领取代次、会话或采集状态改变后，过期调用不得计数或插入命令。"""
    task = task_snapshot()
    current = deepcopy(task) | change
    repo, _, commands, actions = repository(current, [])

    with pytest.raises(HTTPException) as rejected:
        await admit(repo, task, "fixed-command", command_body(), "operator")

    assert rejected.value.status_code == 409
    assert actions == ["update"]
    assert commands.documents == []


async def test_manual_admission_inserts_fixed_identifier_and_session_bound_fields(admit):
    """成功准入必须使用调用方固定 ID，并完整绑定当前 task/run/session 与操作者。"""
    task = task_snapshot()
    repo, tasks, commands, actions = repository(deepcopy(task), [])

    result = await admit(repo, task, "fixed-command", command_body(), "operator")

    assert actions == ["update", "count", "insert"]
    stored = commands.documents[0]
    assert result == stored
    assert {key: stored[key] for key in (
        "id", "taskId", "runId", "sessionId", "actor", "status", "kind", "command", "newline",
        "delaySeconds", "prompt", "timeoutSeconds",
    )} == {
        "id": "fixed-command", "taskId": "task", "runId": "run", "sessionId": "session",
        "actor": "operator", "status": "QUEUED", "kind": "MANUAL", "command": "ls", "newline": "\n",
        "delaySeconds": 0, "prompt": None, "timeoutSeconds": 30,
    }
    assert "createdAt" in stored
    assert tasks.document["commandClaimVersion"] == 8
