"""验证节点故障转移只消费已确认物理关闭的精确运行收据。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from camera_logs.common.database import now
from camera_logs.node import failover
from camera_logs.node.failover import (
    AUTO_FAILOVER_FENCED,
    consume_confirmed_failovers,
    failover_origin_healthy,
    install_failover_routes,
    persist_self_fenced_receipts,
    request_reachable_worker_fencing,
    retry_self_fence_pending,
)
from camera_logs.node.worker import Worker
from camera_logs.tasks.claim import claim_task
from camera_logs.tasks.scheduler import schedule_once
from fastapi import FastAPI
from pymongo.errors import ConnectionFailure
from test_scheduler_batch import _repository


def _task(identifier="task"):
    return {
        "id": identifier, "ip": "127.0.0.1", "port": 22, "resourceId": "resource", "runId": "old-run", "generation": 3,
        "nodeId": "old-node", "sessionId": "old-session", "status": "BLOCKED", "desiredState": "RUNNING",
    }


def _receipt(task, **changes):
    return {
        "taskId": task["id"], "runId": task["runId"], "generation": task["generation"],
        "nodeId": task["nodeId"], "sessionId": task["sessionId"], "instanceId": "old-worker",
        "closedAt": now(), "reason": "AUTO_FAILOVER_FENCED",
    } | changes


def test_original_node_health_uses_platform_release_time_not_worker_receipt_clock():
    """Worker 墙上时间快八小时也不能阻断已释放任务的原节点稳定恢复。"""
    task = _task() | {
        "nodeId": None,
        "failoverExcludedNodeId": "old-node",
        "failoverReleasedAt": now() - timedelta(seconds=1),
    }
    task["closedReceipt"] = _receipt(task | {"nodeId": "old-node"}, closedAt=now() + timedelta(hours=8))
    node = {"id": "old-node", "heartbeat": now()}
    assert failover_origin_healthy(task, node)


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_confirmed_failover_receipt_releases_old_owner_and_excludes_old_node(tmp_path):
    """自动迁移仅在完整收据后释放旧运行，下一次领取不可回到旧节点。"""
    repo = await _repository(tmp_path)
    task = _task()
    await repo.db.resources.insert_one({"id": "resource", "ip": "127.0.0.1", "deletedAt": None, "healthStatus": "ONLINE"})
    await repo.db.tasks.insert_one(task | {"closedReceipt": _receipt(task, closedAt=now() - timedelta(seconds=1))})
    await repo.db.runs.insert_one({"id": task["runId"], "taskId": task["id"]})
    await repo.db.endpoint_locks.insert_one({"taskId": task["id"], "runId": task["runId"]})

    assert await consume_confirmed_failovers(repo) == 1

    migrated = await repo.db.tasks.find_one({"id": task["id"]})
    assert (migrated["status"], migrated["desiredState"], migrated["nodeId"]) == ("STOPPED", "RUNNING", None)
    assert migrated["failoverExcludedNodeId"] == "old-node"
    assert migrated["failoverFromNodeId"] == "old-node"
    assert await repo.db.endpoint_locks.find_one({"taskId": task["id"]}) is None
    assert (await repo.db.runs.find_one({"id": task["runId"]}))["endedAt"]

    await repo.db.nodes.insert_one({"id": "old-node", "heartbeat": now(), "diskPercent": 1, "accepting": True, "capacity": 10})
    assert await claim_task(repo, migrated, "old-node") is None
    assert await repo.db.endpoint_locks.find_one({"taskId": task["id"]}) is None

    await repo.db.nodes.insert_many([
        {"id": "new-node", "heartbeat": now(), "diskPercent": 1, "accepting": True, "capacity": 10},
    ])
    await schedule_once(repo)
    reassigned = await repo.db.tasks.find_one({"id": task["id"]})
    assert reassigned["nodeId"] == "new-node" and reassigned["runId"] != task["runId"]
    event = await repo.db.events.find_one({"type": "TASK_AUTO_MIGRATED", "taskId": task["id"]})
    assert event["oldNodeId"] == "old-node" and event["newNodeId"] == "new-node"
    assert event["nodeId"] == "new-node" and event["actor"] == "system"


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_self_fenced_task_returns_to_original_node_only_after_stable_health_without_alternatives(tmp_path):
    """原节点重启后须持续健康；其它节点可领取时仍不回退到原节点。"""
    repo = await _repository(tmp_path)
    task = _task()
    await repo.db.resources.insert_one({"id": "resource", "ip": "127.0.0.1", "deletedAt": None, "healthStatus": "ONLINE"})
    await repo.db.tasks.insert_one(task | {"closedReceipt": _receipt(task, closedAt=now() - timedelta(seconds=1))})
    await repo.db.runs.insert_one({"id": task["runId"], "taskId": task["id"]})
    await repo.db.endpoint_locks.insert_one({"taskId": task["id"], "runId": task["runId"]})
    assert await consume_confirmed_failovers(repo) == 1

    await asyncio.sleep(.01)
    old = {"id": "old-node", "heartbeat": now(), "diskPercent": 1, "accepting": True, "capacity": 10}
    await repo.db.nodes.insert_one(old)
    await schedule_once(repo)
    waiting = await repo.db.tasks.find_one({"id": task["id"]})
    assert waiting["nodeId"] is None
    assert waiting.get("failoverOriginalHealthySince") is not None
    assert await claim_task(repo, waiting, "old-node") is None

    await repo.db.tasks.update_one(
        {"id": task["id"]}, {"$set": {"failoverOriginalHealthySince": now() - timedelta(seconds=31),
                                           "failoverOriginalHealthyObservedAt": now() - timedelta(seconds=16)}},
    )
    stale_observation = await repo.db.tasks.find_one({"id": task["id"]})
    assert await claim_task(repo, stale_observation, "old-node") is None

    await repo.db.tasks.update_one(
        {"id": task["id"]}, {"$set": {"failoverOriginalHealthySince": now() - timedelta(seconds=31),
                                           "failoverOriginalHealthyObservedAt": now()}},
    )
    await repo.db.nodes.insert_one({"id": "new-node", "heartbeat": now(), "diskPercent": 1, "accepting": True, "capacity": 10})
    await schedule_once(repo)
    assert (await repo.db.tasks.find_one({"id": task["id"]}))["nodeId"] == "new-node"


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_self_fenced_task_can_return_to_stably_recovered_original_node_when_no_alternative_exists(tmp_path):
    """没有其它完整候选时，精确收据和持续健康允许原节点恢复采集。"""
    repo = await _repository(tmp_path)
    task = _task()
    await repo.db.resources.insert_one({"id": "resource", "ip": "127.0.0.1", "deletedAt": None, "healthStatus": "ONLINE"})
    await repo.db.tasks.insert_one(task | {"closedReceipt": _receipt(task, closedAt=now() - timedelta(seconds=1))})
    await repo.db.runs.insert_one({"id": task["runId"], "taskId": task["id"]})
    await repo.db.endpoint_locks.insert_one({"taskId": task["id"], "runId": task["runId"]})
    assert await consume_confirmed_failovers(repo) == 1

    await asyncio.sleep(.01)
    await repo.db.nodes.insert_one({"id": "old-node", "heartbeat": now(), "diskPercent": 1, "accepting": True, "capacity": 10})
    await schedule_once(repo)
    await repo.db.tasks.update_one(
        {"id": task["id"]}, {"$set": {"failoverOriginalHealthySince": now() - timedelta(seconds=31),
                                           "failoverOriginalHealthyObservedAt": now()}},
    )
    await schedule_once(repo)
    returned = await repo.db.tasks.find_one({"id": task["id"]})
    assert returned["nodeId"] == "old-node" and returned["runId"] != task["runId"]
    assert "failoverExcludedNodeId" not in returned


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_legacy_released_task_uses_platform_observation_start_despite_fast_worker_clock(tmp_path):
    """旧记录补写 Mongo 释放时间，Worker 快八小时也需从新心跳开始稳定观察。"""
    repo = await _repository(tmp_path)
    task = _task() | {"nodeId": None, "status": "STOPPED"}
    task["closedReceipt"] = _receipt(task | {"nodeId": "old-node"}, closedAt=now() + timedelta(hours=8))
    task["failoverExcludedNodeId"] = "old-node"
    await repo.db.resources.insert_one({"id": "resource", "ip": "127.0.0.1", "deletedAt": None, "healthStatus": "ONLINE"})
    await repo.db.tasks.insert_one(task)
    await repo.db.nodes.insert_one({"id": "old-node", "heartbeat": now() - timedelta(seconds=1),
                                    "diskPercent": 1, "accepting": True, "capacity": 10})

    await schedule_once(repo)
    seeded = await repo.db.tasks.find_one({"id": task["id"]})
    assert seeded.get("failoverReleasedAt") is not None
    assert seeded.get("failoverOriginalHealthySince") is None

    # MongoDB 日期按毫秒保存，显式跨过补写释放时刻以模拟下一次心跳。
    await repo.db.nodes.update_one(
        {"id": "old-node"}, {"$set": {"heartbeat": now() + timedelta(seconds=1)}},
    )
    await schedule_once(repo)
    observed = await repo.db.tasks.find_one({"id": task["id"]})
    assert observed.get("failoverOriginalHealthySince") is not None
    assert observed["nodeId"] is None

    await repo.db.tasks.update_one(
        {"id": task["id"]}, {"$set": {"failoverOriginalHealthySince": now() - timedelta(seconds=31),
                                        "failoverOriginalHealthyObservedAt": now()}},
    )
    await schedule_once(repo)
    returned = await repo.db.tasks.find_one({"id": task["id"]})
    assert returned["nodeId"] == "old-node" and returned["runId"] != task["runId"]


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_missing_or_untrusted_receipt_keeps_old_owner_blocked(tmp_path):
    """失联、普通收据或错误来源均不能作为自动迁移的关闭证明。"""
    repo = await _repository(tmp_path)
    task = _task()
    receipt = _receipt(task, reason="MANUAL_CLOSE")
    await repo.db.tasks.insert_one(task | {"closedReceipt": receipt})
    await repo.db.endpoint_locks.insert_one({"taskId": task["id"], "runId": task["runId"]})

    assert await consume_confirmed_failovers(repo) == 0
    retained = await repo.db.tasks.find_one({"id": task["id"]})
    assert retained["nodeId"] == "old-node" and retained["status"] == "BLOCKED"
    assert retained["closedReceipt"]["reason"] == receipt["reason"]
    assert await repo.db.endpoint_locks.find_one({"taskId": task["id"], "runId": task["runId"]}) is not None


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_stale_planned_restart_without_closed_receipt_still_blocks_the_old_owner(tmp_path):
    """计划重启标记不等于物理关闭证明，强制终止的旧锁必须进入隔离流程。"""
    repo = await _repository(tmp_path)
    task = _task() | {"status": "COLLECTING", "plannedRestart": True}
    await repo.db.tasks.insert_one(task)
    await repo.db.endpoint_locks.insert_one({"taskId": task["id"], "runId": task["runId"]})
    await repo.db.nodes.insert_one({
        "id": "old-node", "heartbeat": now() - timedelta(minutes=1), "diskPercent": 1, "accepting": False,
    })

    await schedule_once(repo)

    retained = await repo.db.tasks.find_one({"id": task["id"]})
    assert retained["status"] == "BLOCKED"
    assert await repo.db.endpoint_locks.find_one({"taskId": task["id"], "runId": task["runId"]}) is not None


@pytest.mark.usefixtures("mock_claim_transaction")
async def test_failed_failover_transaction_does_not_block_normal_task_scheduling(tmp_path, monkeypatch):
    """单条故障转移事务断库时保留旧锁并退避，健康任务仍可在本轮领取。"""
    repo = await _repository(tmp_path)
    failed = _task("failed")
    normal = _task("normal") | {"nodeId": None, "status": "STOPPED", "runId": "normal-old"}
    await repo.db.resources.insert_one({"id": "resource", "ip": "127.0.0.1", "deletedAt": None, "healthStatus": "ONLINE"})
    await repo.db.tasks.insert_one(failed | {"closedReceipt": _receipt(failed)})
    await repo.db.tasks.insert_one(normal)
    await repo.db.runs.insert_one({"id": failed["runId"], "taskId": failed["id"]})
    await repo.db.endpoint_locks.insert_one({"taskId": failed["id"], "runId": failed["runId"]})
    await repo.db.nodes.insert_one({"id": "healthy-node", "heartbeat": now(), "diskPercent": 1,
                                    "accepting": True, "capacity": 10})

    from camera_logs.common import audited_mutations
    original = audited_mutations.mutation_transaction

    async def fail_consumption(_repo, _callback):
        raise ConnectionFailure("simulated failover transaction outage")

    monkeypatch.setattr(audited_mutations, "mutation_transaction", fail_consumption)
    await schedule_once(repo)
    monkeypatch.setattr(audited_mutations, "mutation_transaction", original)

    retained = await repo.db.tasks.find_one({"id": failed["id"]})
    claimed = await repo.db.tasks.find_one({"id": normal["id"]})
    assert retained["nodeId"] == "old-node" and retained["status"] == "BLOCKED"
    assert retained.get("failoverConsumeRetryAt") is not None
    assert await repo.db.endpoint_locks.find_one({"taskId": failed["id"], "runId": failed["runId"]}) is not None
    assert claimed["nodeId"] == "healthy-node" and claimed["runId"] != normal["runId"]


async def test_self_fenced_runtime_writes_receipt_only_after_database_recovery(tmp_path):
    """Worker 自围栏先物理关闭，数据库恢复后才补写可消费的关闭证明。"""
    repo = await _repository(tmp_path)
    task = _task() | {"status": "COLLECTING"}
    await repo.db.tasks.insert_one(task)
    runtime = type("Runtime", (), {"task": task, "collector": type("Collector", (), {"session_id": "old-session"})(), "stop": AsyncMock()})()
    worker = Worker(repo)
    worker.active[task["id"]] = runtime

    await worker.isolate_active_sessions()
    await asyncio.gather(*worker.releases.values())
    assert runtime.stop.await_count == 1 and task["id"] not in worker.active
    assert worker.self_fenced[task["id"]] is runtime
    assert (await repo.db.tasks.find_one({"id": task["id"]})).get("closedReceipt") is None

    await persist_self_fenced_receipts(worker)
    stored = await repo.db.tasks.find_one({"id": task["id"]})
    assert stored["status"] == "BLOCKED"
    assert stored["closedReceipt"]["reason"] == AUTO_FAILOVER_FENCED


async def test_blocked_task_remains_eligible_for_bounded_remote_fencing_retry(tmp_path):
    """首次探测未获证明后的 BLOCKED 任务在退避到期时仍会再次请求旧 Worker。"""
    repo = await _repository(tmp_path)
    repo.settings.internal_token = "internal-test"
    task = _task() | {"closedReceipt": None}
    await repo.db.tasks.insert_one(task)
    await repo.db.nodes.insert_one({"id": "old-node", "url": "http://old-node", "heartbeat": now(), "accepting": True})
    await repo.db.node_configs.insert_one({"id": "old-node", "url": "http://old-node", "deletedAt": None})
    receipt = _receipt(task) | {"closedAt": now().isoformat()}
    response = httpx.Response(200, json={"closed": True, "closedReceipt": receipt},
                              request=httpx.Request("POST", "http://old-node/internal/failover/fence"))
    post = AsyncMock(return_value=response)
    repo.node_http = SimpleNamespace(post=post)

    assert await request_reachable_worker_fencing(repo, [await repo.db.nodes.find_one({"id": "old-node"})]) == 1
    assert post.await_count == 1


async def test_cancelled_fence_requests_keep_retry_reservation(tmp_path, monkeypatch):
    """全局预算取消慢请求后，已预留的 owner 仍在退避期，下一轮可继续尝试。"""
    repo = await _repository(tmp_path)
    repo.settings.internal_token = "internal-test"
    task = _task() | {"closedReceipt": None}
    await repo.db.tasks.insert_one(task)
    await repo.db.nodes.insert_one({"id": "old-node", "url": "http://old-node", "heartbeat": now(), "accepting": True})
    await repo.db.node_configs.insert_one({"id": "old-node", "url": "http://old-node", "deletedAt": None})
    cancelled = asyncio.Event()

    async def blocked(*_args, **_kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    repo.node_http = SimpleNamespace(post=blocked)
    monkeypatch.setattr(failover, "FENCE_CYCLE_SECONDS", .01)

    assert await request_reachable_worker_fencing(repo, [await repo.db.nodes.find_one({"id": "old-node"})]) == 0
    assert cancelled.is_set()
    retained = await repo.db.tasks.find_one({"id": task["id"]})
    assert retained.get("failoverFenceRetryAt") is not None


async def test_success_status_without_exact_receipt_keeps_fence_retry(tmp_path):
    """旧 Worker 的任意 2xx 不足以证明关闭，必须保留 owner 的下一次围栏机会。"""
    repo = await _repository(tmp_path)
    repo.settings.internal_token = "internal-test"
    task = _task() | {"closedReceipt": None}
    await repo.db.tasks.insert_one(task)
    node = {"id": "old-node", "url": "http://old-node", "heartbeat": now(), "accepting": True}
    await repo.db.nodes.insert_one(node)
    await repo.db.node_configs.insert_one({"id": "old-node", "url": "http://old-node", "deletedAt": None})
    repo.node_http = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(
        200, json={"closed": True}, request=httpx.Request("POST", "http://old-node/internal/failover/fence"),
    )))

    assert await request_reachable_worker_fencing(repo, [node]) == 0
    assert (await repo.db.tasks.find_one({"id": task["id"]})).get("failoverFenceRetryAt") is not None


async def test_self_fencing_starts_every_stop_when_one_runtime_hangs(tmp_path):
    """单个停止永久等待时，其它会话仍立即开始关闭且不会重复创建关闭任务。"""
    repo = await _repository(tmp_path)
    worker = Worker(repo)
    started = asyncio.Event()

    async def blocked():
        started.set()
        await asyncio.Event().wait()

    blocked_runtime = SimpleNamespace(stop=blocked)
    other = [SimpleNamespace(stop=AsyncMock()) for _ in range(9)]
    worker.active = {"blocked": blocked_runtime} | {f"task-{index}": runtime for index, runtime in enumerate(other)}

    await worker.isolate_active_sessions()
    assert started.is_set() and all(runtime.stop.await_count == 1 for runtime in other)
    assert len(worker.releases) == 10
    for task in worker.releases.values():
        task.cancel()
    await asyncio.gather(*worker.releases.values(), return_exceptions=True)


async def test_failed_self_fence_retries_only_its_own_runtime(tmp_path):
    """自围栏收尾失败后保持专属标记，恢复时不能由普通 release 改写运行意图。"""
    repo = await _repository(tmp_path)
    task = _task() | {"status": "COLLECTING"}
    await repo.db.tasks.insert_one(task)
    stop = AsyncMock(side_effect=[OSError("archive publish failed"), None])
    runtime = SimpleNamespace(task=task, collector=SimpleNamespace(session_id="old-session"), stop=stop,
                              retired=False, stopping=False)
    worker = Worker(repo)
    worker.active[task["id"]] = runtime

    await worker.isolate_active_sessions()
    assert worker.self_fence_pending[task["id"]] is runtime and task["id"] in worker.active
    worker.releases.clear()
    await retry_self_fence_pending(worker)
    await asyncio.gather(*worker.releases.values())
    assert stop.await_count == 2 and task["id"] not in worker.active
    assert worker.self_fenced[task["id"]] is runtime and not worker.self_fence_pending


async def test_internal_fence_rejects_missing_token_and_wrong_owner(tmp_path):
    """内部关闭端点必须在查询或停止前拒绝未认证和节点身份不匹配请求。"""
    repo = await _repository(tmp_path)
    repo.settings.node_id = "old-node"
    worker = Worker(repo)
    app = FastAPI()
    app.state.worker = worker
    install_failover_routes(app, SimpleNamespace(internal_token="internal-test"))
    transport = httpx.ASGITransport(app=app)
    body = {"taskId": "task", "runId": "old-run", "generation": 3, "nodeId": "old-node"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.post("/internal/failover/fence", json=body)).status_code == 401
        assert (await client.post("/internal/failover/fence", headers={"Authorization": "Bearer internal-test"},
                                  json={**body, "nodeId": "another-node"})).status_code == 409


async def test_internal_fence_allows_one_close_and_rejects_concurrent_request(tmp_path):
    """精确 owner 的并发 fence 只允许一个物理 stop，另一请求明确冲突。"""
    repo = await _repository(tmp_path)
    repo.settings.node_id = "old-node"
    task = _task() | {"status": "COLLECTING"}
    await repo.db.tasks.insert_one(task)
    worker = Worker(repo)
    started, release = asyncio.Event(), asyncio.Event()

    async def stop():
        started.set()
        await release.wait()

    runtime = SimpleNamespace(task=task, collector=SimpleNamespace(session_id="old-session"), stop=stop,
                              retired=False, stopping=False)
    worker.active[task["id"]] = runtime
    app = FastAPI()
    app.state.worker = worker
    install_failover_routes(app, SimpleNamespace(internal_token="internal-test"))
    transport = httpx.ASGITransport(app=app)
    body = {"taskId": task["id"], "runId": task["runId"], "generation": task["generation"], "nodeId": "old-node"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(client.post("/internal/failover/fence", json=body,
                                                headers={"Authorization": "Bearer internal-test"}))
        await started.wait()
        second = await client.post("/internal/failover/fence", json=body,
                                   headers={"Authorization": "Bearer internal-test"})
        release.set()
        completed = await first
    assert completed.status_code == 200 and second.status_code == 409
    assert runtime.retired and runtime.stopping and task["id"] not in worker.releases
    assert (await repo.db.tasks.find_one({"id": task["id"]}))["closedReceipt"]["reason"] == AUTO_FAILOVER_FENCED


async def test_normal_isolation_preserves_exact_self_fenced_receipt(tmp_path):
    """后台隔离不得覆盖同一实例已经写入的自动迁移关闭原因。"""
    repo = await _repository(tmp_path)
    worker = Worker(repo)
    task = _task() | {"status": "COLLECTING", "closedReceipt": _receipt(_task(), instanceId=worker.instance_id)}
    await repo.db.tasks.insert_one(task)
    runtime = SimpleNamespace(task=task, collector=SimpleNamespace(session_id="old-session"), stop=AsyncMock(),
                              retired=False, stopping=False)
    await worker.finish_runtime(runtime, "isolate")
    receipt = (await repo.db.tasks.find_one({"id": task["id"]}))["closedReceipt"]
    assert receipt["reason"] == AUTO_FAILOVER_FENCED


async def test_preconnect_reconciled_isolation_never_writes_closed_receipt(tmp_path):
    """预建连未知已用名额代次协调时，即使保留了会话对象也不能伪造关闭证明。"""
    repo = await _repository(tmp_path)
    worker = Worker(repo)
    task = _task() | {"status": "COLLECTING"}
    await repo.db.tasks.insert_one(task)
    runtime = SimpleNamespace(task=task, collector=SimpleNamespace(session_id="not-connected"), stop=AsyncMock(),
                              retired=False, stopping=False, preconnect_slot_reconciled=True)

    await worker.finish_runtime(runtime, "isolate")

    assert not (await repo.db.tasks.find_one({"id": task["id"]})).get("closedReceipt")


async def test_preconnect_reconciled_recovery_rejects_synthetic_closed_receipt(tmp_path):
    """恢复路径同样不能把仅构造的采集器对象当作已经关闭的 socket。"""
    repo = await _repository(tmp_path)
    worker = Worker(repo)
    task = _task() | {"status": "BLOCKED", "sessionId": "not-connected"}
    await repo.db.tasks.insert_one(task)
    runtime = SimpleNamespace(task=task, collector=SimpleNamespace(session_id="not-connected"), stop=AsyncMock(),
                              preconnect_slot_reconciled=True)

    with pytest.raises(RuntimeError, match="缺少已关闭会话"):
        await worker.recover_blocked_runtime(runtime)

    assert not (await repo.db.tasks.find_one({"id": task["id"]})).get("closedReceipt")
