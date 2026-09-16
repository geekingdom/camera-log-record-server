"""节点失联后的保守故障转移：仅消费已证明物理关闭的当前运行。"""

import asyncio
import logging
import secrets
from datetime import timedelta

import httpx
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.ownership import owner_filter
from camera_logs.node.recovery import finish_blocked_run, matching_closed_receipt
from camera_logs.tasks.control import _guard_resources

AUTO_FAILOVER_FENCED = "AUTO_FAILOVER_FENCED"
FENCE_MAX_CANDIDATES = 32
FENCE_CONCURRENCY = 8
FENCE_CYCLE_SECONDS = 1
FENCE_RETRY_SECONDS = 15
FAILOVER_CONSUME_MAX = 100
logger = logging.getLogger(__name__)


class FailoverFenceRequest(BaseModel):
    """内部关闭请求只能引用当前精确运行，不能携带设备地址或任意控制参数。"""
    model_config = ConfigDict(extra="forbid")
    taskId: str = Field(min_length=1, max_length=128)
    runId: str = Field(min_length=1, max_length=128)
    generation: int
    nodeId: str = Field(min_length=1, max_length=128)


def is_confirmed_failover_receipt(task):
    """自动迁移只接受 self-fencing 或内部关闭接口写入的精确关闭收据。"""
    return matching_closed_receipt(task) and (task.get("closedReceipt") or {}).get("reason") == AUTO_FAILOVER_FENCED


def is_exact_fence_response(task, payload):
    """仅信任旧 Worker 返回的完整自围栏收据，任意 2xx 都不能代替关闭证明。"""
    if not isinstance(payload, dict) or payload.get("closed") is not True:
        return False
    receipt = payload.get("closedReceipt")
    if not isinstance(receipt, dict):
        return False
    return receipt.get("reason") == AUTO_FAILOVER_FENCED and receipt.get("taskId") == task.get("id") and all(
        receipt.get(key) == task.get(key) for key in ("runId", "generation", "nodeId", "sessionId")
    ) and bool(receipt.get("instanceId")) and receipt.get("closedAt") is not None


async def request_reachable_worker_fencing(repo, stale_nodes):
    """在小预算内并发请求失联节点关闭；失败退避但仍保留后续重试资格。"""
    client = getattr(repo, "node_http", None)
    if client is None or not repo.settings.internal_token:
        return 0
    configs = {item["id"]: item async for item in repo.db.node_configs.find({"deletedAt": None})}
    candidates = []
    for node in stale_nodes:
        if len(candidates) >= FENCE_MAX_CANDIDATES:
            break
        config = configs.get(node["id"])
        if config is None or node.get("url") != config.get("url"):
            continue
        retryable = {"$or": [{"failoverFenceRetryAt": {"$exists": False}}, {"failoverFenceRetryAt": {"$lte": now()}}]}
        async for task in repo.db.tasks.find({"nodeId": node["id"], "desiredState": "RUNNING", **retryable}).limit(FENCE_MAX_CANDIDATES):
            if not all(task.get(key) is not None for key in ("runId", "generation", "sessionId")):
                continue
            candidates.append((node, config, task))
            if len(candidates) >= FENCE_MAX_CANDIDATES:
                break
    gate = asyncio.Semaphore(FENCE_CONCURRENCY)

    # 网络调用被全局周期预算取消时，协程没有机会进入异常分支。先以精确 owner
    # 预留退避，才能保证慢旧节点不会被下一批候选持续挤占而永久饥饿。
    reserved = []
    retry_at = now() + timedelta(seconds=FENCE_RETRY_SECONDS)
    for node, config, task in candidates:
        claimed = await repo.db.tasks.update_one(
            owner_filter(task) | {
                "$or": [
                    {"failoverFenceRetryAt": {"$exists": False}},
                    {"failoverFenceRetryAt": {"$lte": now()}},
                ]
            },
            {"$set": {"failoverFenceRetryAt": retry_at}},
        )
        if claimed.matched_count:
            reserved.append((node, config, task))

    async def request(node, config, task):
        async with gate:
            try:
                response = await client.post(
                    config["url"].rstrip("/") + "/internal/failover/fence",
                    headers={"Authorization": "Bearer " + repo.settings.internal_token},
                    json={"taskId": task["id"], "runId": task["runId"], "generation": task["generation"], "nodeId": node["id"]},
                    timeout=FENCE_CYCLE_SECONDS,
                )
                response.raise_for_status()
                if not is_exact_fence_response(task, response.json()):
                    raise RuntimeError("旧节点未返回精确自围栏关闭收据")
            except (httpx.HTTPError, RuntimeError, TimeoutError, ValueError) as error:
                logger.info("失联节点未取得关闭证明 node=%s task=%s error_type=%s", node["id"], task["id"], type(error).__name__)
                return False
            await repo.db.tasks.update_one(owner_filter(task), {"$unset": {"failoverFenceRetryAt": ""}})
            return True

    try:
        results = await asyncio.wait_for(asyncio.gather(*(request(*candidate) for candidate in reserved)), FENCE_CYCLE_SECONDS)
    except TimeoutError:
        return 0
    return sum(results)


async def consume_confirmed_failovers(repo):
    """原子释放有关闭证明的失联运行，保留旧节点排除直到新运行被领取。"""
    retryable = {"$or": [{"failoverConsumeRetryAt": {"$exists": False}}, {"failoverConsumeRetryAt": {"$lte": now()}}]}
    candidates = [task async for task in repo.db.tasks.find({
        "status": "BLOCKED", "desiredState": "RUNNING", "closedReceipt.reason": AUTO_FAILOVER_FENCED, **retryable,
    }).sort([("updatedAt", 1), ("id", 1)]).limit(FAILOVER_CONSUME_MAX)]
    consumed = 0
    for candidate in candidates:
        async def commit(session, task_id=candidate["id"]):
            task = await repo.db.tasks.find_one_and_update(
                {"id": task_id, "status": "BLOCKED", "desiredState": "RUNNING"},
                {"$inc": {"controlClaimVersion": 1}}, return_document=True, session=session,
            )
            if task is None or not is_confirmed_failover_receipt(task) or task.get("resourceDeleted"):
                return False
            await _guard_resources(repo.db, task, session)
            old_node_id = task["nodeId"]
            await finish_blocked_run(repo, task, session, restart=True)
            changed = await repo.db.tasks.update_one(
                {"id": task["id"], "nodeId": None, "status": "STOPPED", "desiredState": "RUNNING"},
                {"$set": {"failoverExcludedNodeId": old_node_id, "failoverFromNodeId": old_node_id,
                          "failoverReason": AUTO_FAILOVER_FENCED, "updatedAt": now()}}, session=session,
            )
            if not changed.matched_count:
                raise HTTPException(409, "故障转移归属已变化")
            return True

        try:
            if await audited_mutations.mutation_transaction(repo, commit):
                consumed += 1
            else:
                await repo.db.tasks.update_one(owner_filter(candidate), {"$set": {
                    "failoverConsumeRetryAt": now() + timedelta(seconds=FENCE_RETRY_SECONDS),
                }})
        except HTTPException as error:
            logger.info("故障转移收据暂不可消费 task=%s status=%s", candidate["id"], error.status_code)
            await repo.db.tasks.update_one(owner_filter(candidate), {"$set": {
                "failoverConsumeRetryAt": now() + timedelta(seconds=FENCE_RETRY_SECONDS),
            }})
    return consumed


async def close_for_failover(worker, task):
    """由旧 Worker 关闭完全匹配的本机连接并写入可消费收据，失败不产生证明。"""
    if task["id"] in worker.releases:
        raise HTTPException(409, "旧运行正在收尾，不能并发关闭")
    runtime = worker.active.get(task["id"])
    if runtime is None or owner_filter(runtime.task) != owner_filter(task):
        raise HTTPException(409, "旧运行不在当前Worker，不能确认关闭")
    closing = asyncio.get_running_loop().create_future()
    worker.releases[task["id"]] = closing
    try:
        runtime.retired = runtime.stopping = True
        await runtime.stop()
        worker.self_fenced[task["id"]] = runtime
        if worker.active.get(task["id"]) is runtime:
            worker.active.pop(task["id"])
        from camera_logs.node.recovery import record_closed_receipt
        collector = getattr(runtime, "collector", None)
        session_id = getattr(collector, "session_id", None)
        await record_closed_receipt(worker.repo, runtime.task, worker.instance_id, session_id, reason=AUTO_FAILOVER_FENCED)
        changed = await worker.repo.db.tasks.update_one(
            owner_filter(runtime.task) | {"desiredState": "RUNNING"},
            {"$set": {"status": "BLOCKED", "error": "节点失联，已确认关闭，等待自动迁移", "updatedAt": now()}},
        )
        if not changed.matched_count:
            raise HTTPException(409, "用户控制意图已变化，不能自动迁移")
        worker.self_fenced.pop(task["id"], None)
        worker.discard_closed(runtime)
        closing.set_result(True)
        return {"closed": True, "closedReceipt": {
            "taskId": task["id"], "runId": task["runId"], "generation": task["generation"],
            "nodeId": task["nodeId"], "sessionId": session_id, "instanceId": worker.instance_id,
            "closedAt": now(), "reason": AUTO_FAILOVER_FENCED,
        }}
    except BaseException:
        if not closing.done():
            closing.set_result(False)
        raise
    finally:
        if worker.releases.get(task["id"]) is closing:
            worker.releases.pop(task["id"], None)


async def persist_self_fenced_receipts(worker):
    """数据库恢复后仅为本进程确实停止成功的旧会话补写关闭收据。"""
    for task_id, runtime in list(worker.self_fenced.items()):
        collector = getattr(runtime, "collector", None)
        session_id = getattr(collector, "session_id", None)
        if not session_id:
            worker.self_fenced.pop(task_id, None)
            continue
        try:
            await record_self_fenced_receipt(worker, runtime, session_id)
        except Exception:
            logger.exception("self-fencing关闭收据暂不能写入 task=%s", task_id)
            continue
        worker.self_fenced.pop(task_id, None)


async def self_fence_active_sessions(worker):
    """数据库长期失联时关闭本机连接；停止失败没有任何自动迁移资格。"""
    await self_fence_active_sessions_for(worker, list(worker.active.items()))


async def retry_self_fence_pending(worker):
    """数据库恢复后只重试本实例已进入自围栏的关闭，禁止普通收尾抢占。"""
    for task_id, runtime in list(worker.self_fence_pending.items()):
        if worker.active.get(task_id) is not runtime:
            worker.self_fence_pending.pop(task_id, None)
            continue
        if task_id not in worker.releases:
            await self_fence_active_sessions_for(worker, [(task_id, runtime)])


async def self_fence_active_sessions_for(worker, runtimes):
    """为给定实例启动可重入保护的自围栏关闭，不等待可能永久阻塞的收尾。"""
    async def fence(task_id, runtime):
        try:
            await runtime.stop()
        except Exception:
            worker.self_fence_pending[task_id] = runtime
            logger.exception("数据库失联时关闭采集实例失败 task=%s", task_id)
        else:
            if worker.self_fence_pending.get(task_id) is runtime:
                worker.self_fence_pending.pop(task_id, None)
            worker.self_fenced[task_id] = runtime
            if worker.active.get(task_id) is runtime:
                worker.active.pop(task_id, None)

    for task_id, runtime in runtimes:
        if task_id not in worker.releases:
            runtime.retired = runtime.stopping = True
            worker.releases[task_id] = asyncio.create_task(fence(task_id, runtime))
    await asyncio.sleep(0)


async def record_self_fenced_receipt(worker, runtime, session_id):
    """先按精确归属固化收据，再将仍在运行意图的旧任务标为等待迁移。"""
    from camera_logs.node.recovery import record_closed_receipt
    await record_closed_receipt(worker.repo, runtime.task, worker.instance_id, session_id, reason=AUTO_FAILOVER_FENCED)
    await worker.repo.db.tasks.update_one(
        owner_filter(runtime.task) | {"desiredState": "RUNNING"},
        {"$set": {"status": "BLOCKED", "error": "节点数据库失联后已主动关闭，等待自动迁移", "updatedAt": now()}},
    )


def install_failover_routes(app, settings):
    """安装仅调度器可调用的旧 Worker 精确关闭接口。"""
    @app.post("/internal/failover/fence")
    async def fence(body: FailoverFenceRequest, request: Request):
        expected = "Bearer " + settings.internal_token
        if not settings.internal_token or not secrets.compare_digest(request.headers.get("authorization", ""), expected):
            raise HTTPException(401, "内部节点认证失败")
        worker = app.state.worker
        if body.nodeId != worker.repo.settings.node_id:
            raise HTTPException(409, "节点身份不匹配")
        task = await worker.repo.db.tasks.find_one({
            "id": body.taskId, "runId": body.runId, "generation": body.generation, "nodeId": body.nodeId,
            "desiredState": "RUNNING",
        })
        if task is None:
            raise HTTPException(409, "旧运行归属或用户控制意图已变化")
        return await close_for_failover(worker, task)
