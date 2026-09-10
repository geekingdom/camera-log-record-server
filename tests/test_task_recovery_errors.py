"""BLOCKED 恢复端点的重复请求和失败边界回归。"""
# ruff: noqa: F811

import pytest
from camera_logs.common import audited_mutations
from pymongo.errors import ConnectionFailure
from test_api import client  # noqa: F401
from test_task_control import _create_task, _set_task
from test_task_recovery_receipts import _block_with_receipt


@pytest.mark.parametrize("confirmation", [False, True])
def test_restart_reuses_operation_after_old_run_is_released(client, confirmation):
    """关闭已确认并释放归属后，重复请求仍返回原操作，不再要求BLOCKED。"""
    task, repo = _create_task(client), client.app.state.repo
    _block_with_receipt(client, task)
    body = {"confirmIsolation": confirmation, "evidence": "旧实例停止且设备连接已关闭，已完成核验"}
    first = client.post(f"/api/v1/tasks/{task['id']}/restart", json=body)
    current = client.portal.call(repo.db.tasks.find_one, {"id": task["id"]})
    assert current["nodeId"] is None and current["status"] == "STOPPED"
    second = client.post(f"/api/v1/tasks/{task['id']}/restart", json=body)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert client.portal.call(repo.db.operations.count_documents, {"taskId": task["id"], "action": "restart-blocked"}) == 1


@pytest.mark.parametrize("confirmation", [False, True])
def test_recovery_database_failure_returns_unknown_503(client, monkeypatch, confirmation):
    """事务异常不能伪造失败回滚或已启动，两个恢复入口都返回明确未知结果。"""
    task = _create_task(client)
    _set_task(client, task["id"], status="BLOCKED", runId="old", nodeId="node", generation=1)

    async def fail(*_args):
        raise ConnectionFailure("private connection detail")

    monkeypatch.setattr(audited_mutations, "mutation_transaction", fail)
    response = client.post(f"/api/v1/tasks/{task['id']}/restart", json={
        "confirmIsolation": confirmation, "evidence": "旧实例已停止并完成连接核验"})
    assert response.status_code == 503
    assert "未知" in response.text and "private connection detail" not in response.text


def test_confirmation_rejects_blank_evidence_again(client):
    """确认隔离的证据验证在正式路由边界生效。"""
    task = _create_task(client)
    response = client.post(f"/api/v1/tasks/{task['id']}/restart", json={
        "confirmIsolation": True, "evidence": "             ",
    })
    assert response.status_code == 422
