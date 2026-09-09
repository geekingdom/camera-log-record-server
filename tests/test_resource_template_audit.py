"""资源和模板变更的审计事实、幂等重放与扫尾顺序回归。

内存数据库仅验证路由契约，真正的事务回滚由副本集验证脚本覆盖。
"""

import pytest
from test_resources import resource_client  # noqa: F401


def test_resource_delete_audit_precedes_cleanup_and_survives_retry(resource_client, monkeypatch):  # noqa: F811
    """删除意图与审计先提交；扫尾异常不能使已删除资源永久缺失审计。"""
    client = resource_client
    repo = client.app.state.repo
    response = client.post("/api/v1/resources", headers={"Idempotency-Key": "audited-delete"},
                           json={"name": "审计串口", "kind": "SERIAL_SERVER", "ip": "192.0.2.70"})
    assert response.status_code == 201, response.text
    identifier = response.json()["id"]

    async def failing_cleanup(_repo, target):
        event = await repo.db.audit.find_one({"targetId": target, "action": "delete_resource"})
        assert event is not None, "扫尾开始前必须提交删除审计"
        raise RuntimeError("模拟停止扫尾失败")

    monkeypatch.setattr("camera_logs.resources.api.reconcile_resource_deletion", failing_cleanup)
    with pytest.raises(RuntimeError, match="模拟停止扫尾失败"):
        client.delete(f"/api/v1/resources/{identifier}?version=1")
    with pytest.raises(RuntimeError, match="模拟停止扫尾失败"):
        client.delete(f"/api/v1/resources/{identifier}?version=1")
    assert client.portal.call(repo.db.audit.count_documents,
                             {"targetId": identifier, "action": "delete_resource"}) == 1
    stored = client.portal.call(repo.db.resources.find_one, {"id": identifier})
    assert stored["deletedAt"] and stored["deletionState"] == "PENDING"


def test_template_replay_and_conflicts_do_not_add_false_audits(resource_client):  # noqa: F811
    """同键重放、过期版本和删除冲突均不能写入虚假审计。

    不同幂等键的同名唯一索引冲突需要真实 MongoDB 回滚，见
    scripts/verify_audited_mutations.py 的模板唯一性验证。
    """
    client = resource_client
    repo = client.app.state.repo
    path = "/api/v1/command-templates"
    headers = {"Idempotency-Key": "template-audit"}
    response = client.post(path, headers=headers, json={"name": "审计模板"})
    assert response.status_code == 201, response.text
    identifier = response.json()["id"]
    assert client.post(path, headers=headers, json={"name": "审计模板"}).json()["id"] == identifier
    assert client.post(path, headers=headers, json={"name": "其它内容"}).status_code == 409
    assert client.patch(f"{path}/{identifier}", json={"name": "已修改", "version": 1}).status_code == 200
    assert client.patch(f"{path}/{identifier}", json={"name": "过期", "version": 1}).status_code == 409
    assert client.delete(f"{path}/{identifier}?version=1").status_code == 409
    assert client.delete(f"{path}/{identifier}?version=2").status_code == 204

    async def actions():
        return [item["action"] async for item in repo.db.audit.find({"targetId": identifier})]

    assert client.portal.call(actions) == ["create_template", "edit_template", "delete_template"]
