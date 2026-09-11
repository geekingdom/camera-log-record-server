"""验证资源地址限定、专用优先及领取事务中的配置复核。"""

import pytest
from camera_logs.node.health import rank_nodes
from camera_logs.node.resource_routing import accepts_resource, normalize_resource_networks
from camera_logs.tasks.claim import claim_task
from camera_logs.tasks.scheduler import schedule_once
from test_api import client  # noqa: F401
from test_node_health_selection import node
from test_scheduler_batch import _insert_pending_tasks, _repository


def test_network_rules_normalize_ipv4_ipv6_and_reject_invalid():
    assert normalize_resource_networks([" 10.1.2.3 ", "10.1.2.3", "10.1.2.7/24", "2001:db8::1"]) == [
        "10.1.2.3", "10.1.2.0/24", "2001:db8::1"]
    for values in [[""], ["camera.local"], ["10.1.2.3/99"], ["*"]]:
        with pytest.raises(ValueError):
            normalize_resource_networks(values)
    assert accepts_resource({}, None)
    dedicated = {"isGeneralNode": False, "resourceNetworks": ["10.1.2.3", "2001:db8::/64"]}
    assert accepts_resource(dedicated, "10.1.2.3")
    assert accepts_resource(dedicated, "2001:db8::45")
    assert not accepts_resource(dedicated, "10.1.2.4")
    assert not accepts_resource({"isGeneralNode": False}, "10.1.2.3")


def test_dedicated_priority_respects_health_and_uses_resource_not_connection_ip():
    dedicated = node("dedicated", cpu=80) | {"isGeneralNode": False, "resourceNetworks": ["10.1.2.0/24"]}
    general = node("general")
    task = {"resourceIp": "10.1.2.3", "ip": "192.0.2.8", "protocol": "TELNET_SERIAL"}
    assert rank_nodes([general, dedicated], {}, task)[0][1] == "dedicated"
    assert rank_nodes([general, dedicated], {}, task | {"resourceIp": "10.2.0.1"})[0][1] == "general"
    dedicated["telemetry"]["cpuPercent"] = 96
    assert rank_nodes([general, dedicated], {}, task)[0][1] == "general"


def test_node_routing_configuration_requires_admin_and_nonempty_dedicated_rules(client):  # noqa: F811
    payload = {"id": "dedicated", "url": "http://node:18081", "capacity": 100, "isGeneralNode": False}
    assert client.post("/api/v1/admin/nodes", json=payload).status_code == 422
    response = client.post("/api/v1/admin/nodes", json=payload | {"resourceNetworks": ["10.1.2.4/24"]})
    assert response.status_code == 201, response.text
    assert response.json()["resourceNetworks"] == ["10.1.2.0/24"]
    assert response.json()["isGeneralNode"] is False
    assert client.patch("/api/v1/admin/nodes/dedicated", json={"version": 1, "resourceNetworks": []}).status_code == 422
    edited = client.patch("/api/v1/admin/nodes/dedicated", json={"version": 1, "isGeneralNode": True, "resourceNetworks": []})
    assert edited.status_code == 200 and edited.json()["isGeneralNode"] is True
    assert client.patch("/api/v1/admin/nodes/dedicated", json={"version": 1, "isGeneralNode": False}).status_code == 409


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_scheduler_matches_resource_ip_and_does_not_trust_worker_routing(tmp_path):
    repo = await _repository(tmp_path)
    await _insert_pending_tasks(repo, 1)
    await repo.db.tasks.update_one({"id": "task-0"}, {"$set": {"resourceId": "resource", "ip": "192.0.2.8", "protocol": "TELNET_SERIAL"}})
    await repo.db.resources.insert_one({"id": "resource", "ip": "10.1.2.3", "kind": "HIKVISION_NETWORK", "healthStatus": "ONLINE"})
    await repo.db.nodes.insert_many([node("dedicated", cpu=80), node("general") | {"isGeneralNode": False}])
    await repo.db.node_configs.insert_one({"id": "dedicated", "isGeneralNode": False, "resourceNetworks": ["10.1.2.0/24"]})
    await schedule_once(repo)
    assert (await repo.db.tasks.find_one({"id": "task-0"}))["nodeId"] == "dedicated"


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_claim_rejects_changed_rules_before_creating_run(tmp_path):
    repo = await _repository(tmp_path)
    await _insert_pending_tasks(repo, 1)
    await repo.db.nodes.insert_one(node("dedicated"))
    await repo.db.node_configs.insert_one({"id": "dedicated", "isGeneralNode": False, "resourceNetworks": ["203.0.113.0/24"]})
    task = await repo.get("tasks", "task-0")
    assert await claim_task(repo, task, "dedicated") is None
    assert await repo.db.runs.count_documents({}) == 0
    assert await repo.db.endpoint_locks.count_documents({}) == 0
