"""节点软删除须保留历史目录，并在重试、陈旧版本和活动任务下保持一致。"""
# ruff: noqa: F811
from camera_logs.common.database import now
from test_api import client  # noqa: F401


def test_delete_discovered_node_hides_heartbeat_but_preserves_history(client):
    db = client.app.state.repo.db
    client.portal.call(db.nodes.insert_one, {"id": "edge", "url": "http://worker:8001",
                                           "heartbeat": now(), "activeTasks": 0, "capacity": 100})
    client.portal.call(db.files.insert_one, {"id": "historical-file", "nodeId": "edge"})
    for _ in range(2):
        assert client.delete("/api/v1/admin/nodes/edge?version=0").status_code == 204
    assert client.get("/api/v1/admin/nodes").json()["items"] == []
    assert client.portal.call(db.files.count_documents, {"id": "historical-file"}) == 1
    assert client.portal.call(db.nodes.find_one, {"id": "edge"})["url"] == "http://worker:8001"
    assert client.portal.call(db.audit.count_documents, {"action": "delete_node", "targetId": "edge"}) == 1
    assert client.patch("/api/v1/admin/nodes/edge", json={"version": 1, "accepting": True}).status_code == 404
    restored = client.post("/api/v1/admin/nodes", json={"id": "edge", "url": "http://worker:8001", "capacity": 10})
    assert restored.status_code == 201
    assert client.delete("/api/v1/admin/nodes/edge?version=0").status_code == 409


def test_delete_rejects_owned_tasks_paused_runs_and_stale_versions(client):
    db = client.app.state.repo.db
    assert client.post("/api/v1/admin/nodes", json={"id": "edge", "url": "http://worker:8001", "capacity": 10}).status_code == 201
    client.portal.call(db.tasks.insert_one, {"id": "pending", "nodeId": "edge", "status": "PENDING"})
    assert client.delete("/api/v1/admin/nodes/edge?version=1").status_code == 409
    client.portal.call(db.tasks.delete_many, {})
    client.portal.call(db.runs.insert_one, {"id": "paused", "nodeId": "edge"})
    assert client.delete("/api/v1/admin/nodes/edge?version=1").status_code == 409
    client.portal.call(db.runs.update_one, {"id": "paused"}, {"$set": {"endedAt": now()}})
    assert client.delete("/api/v1/admin/nodes/edge?version=2").status_code == 409
    assert client.delete("/api/v1/admin/nodes/edge?version=1").status_code == 204
    assert client.delete("/api/v1/admin/nodes/missing?version=0").status_code == 404
    assert client.delete("/api/v1/admin/nodes/edge").status_code == 422
