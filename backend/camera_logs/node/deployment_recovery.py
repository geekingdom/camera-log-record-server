"""受控 Worker 部署的旧运行关闭证明与保守调度释放。

部署脚本用新镜像先执行 prepare，冻结旧 Worker 当前拥有的精确运行身份；在 Docker
实际确认旧容器停止后执行 complete。关闭证明可供原始日志归档恢复使用，而任务锁仅在
用户仍保持 RUNNING 意图且归属未变化时释放，未知连接、普通失联和用户暂停均不推断为
可重新采集。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
from typing import Any

from pymongo import AsyncMongoClient

from camera_logs.common import audited_mutations
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.common.models import new_id

_TOKEN = re.compile(r"[0-9a-f]{32}")
_IDENTITY = ("id", "taskId", "runId", "nodeId", "generation", "sessionId")
_FILE_IDENTITY = ("id", "path", "sessionId")


def _task_identity(task: dict[str, Any]) -> dict[str, Any] | None:
    """提取可证明的旧 owner 身份，缺少任何字段时不建立部署恢复前提。"""
    values = {
        "id": task.get("id"), "taskId": task.get("id"), "runId": task.get("runId"),
        "nodeId": task.get("nodeId"), "generation": task.get("generation"),
        "sessionId": task.get("sessionId"),
    }
    return values if all(values[key] is not None for key in _IDENTITY) else None


def _owner_filter(identity: dict[str, Any]) -> dict[str, Any]:
    """构造包含会话代次的完整 owner 条件，不能退化为仅 task 或 run 标识。"""
    return {"id": identity["taskId"], "runId": identity["runId"], "nodeId": identity["nodeId"],
            "generation": identity["generation"], "sessionId": identity["sessionId"]}


def _run_owner_filter(identity: dict[str, Any]) -> dict[str, Any]:
    """匹配 run 的持久 owner；会话由部署令牌在关闭证明时一次性固化。"""
    return {"id": identity["runId"], "taskId": identity["taskId"], "nodeId": identity["nodeId"],
            "generation": identity["generation"]}


async def prepare_deployment_recovery(repo: Any, node_id: str, *, token: str | None = None) -> dict[str, Any]:
    """冻结目标节点所有精确旧 owner，返回部署脚本后续必须携带的随机令牌。"""
    token = token or new_id()
    if not _TOKEN.fullmatch(token):
        raise ValueError("部署恢复令牌格式无效")
    result: Counter[str] = Counter()
    prepared_at = now().isoformat()
    candidates = [task async for task in repo.db.tasks.find({"nodeId": node_id})]
    for task in candidates:
        identity = _task_identity(task)
        if identity is None:
            if task.get("status") == "PENDING" and task.get("runId") is None and task.get("sessionId") is None:
                released = await repo.db.tasks.update_one(
                    {"id": task.get("id"), "nodeId": node_id, "status": "PENDING", "runId": None},
                    {"$set": {"nodeId": None, "status": "STOPPED", "updatedAt": now()}},
                )
                result["pendingReleased" if released.modified_count else "ownerChanged"] += 1
                continue
            result["incompleteOwner"] += 1
            continue
        marker = identity | {
            "token": token, "preparedAt": prepared_at, "desiredState": task.get("desiredState"),
            "controlClaimVersion": task.get("controlClaimVersion"),
            "controlOperationId": task.get("controlOperationId"),
        }

        async def commit(session, *, snapshot=identity, recovery_marker=marker):
            """同一事务确认 run 身份和任务 owner，再写入两侧令牌标记。"""
            run = await repo.db.runs.find_one(_run_owner_filter(snapshot), session=session)
            if run is None:
                return "missingRun"
            if run.get("sessionId") not in {None, snapshot["sessionId"]}:
                return "runSessionConflict"
            changed = await repo.db.tasks.update_one(
                _owner_filter(snapshot),
                {"$set": {"deploymentRecovery": recovery_marker}}, session=session,
            )
            if not changed.matched_count:
                return "ownerChanged"
            changed = await repo.db.runs.update_one(
                _run_owner_filter(snapshot) | {"sessionId": run.get("sessionId")},
                {"$set": {"deploymentRecovery": recovery_marker}}, session=session,
            )
            if not changed.matched_count:
                raise RuntimeError("部署预备运行归属在事务中变化")
            files = [item async for item in repo.db.files.find({
                "taskId": snapshot["taskId"], "runId": snapshot["runId"],
                "nodeId": snapshot["nodeId"], "status": "OPEN",
            }, session=session)]
            for file in files:
                if any(file.get(key) in (None, "") for key in _FILE_IDENTITY):
                    raise RuntimeError("部署预备原始日志身份不完整")
                file_marker = recovery_marker | {
                    "fileId": file["id"], "path": file["path"], "sessionId": file["sessionId"],
                    "createdAt": file.get("createdAt"), "watermark": file.get("bytes"),
                    "firstSequence": file.get("firstSequence"),
                }
                changed = await repo.db.files.update_one(
                    {"id": file["id"], "path": file["path"], "sessionId": file["sessionId"],
                     "status": "OPEN"},
                    {"$set": {"deploymentRecovery": file_marker}}, session=session,
                )
                if not changed.matched_count:
                    raise RuntimeError("部署预备原始日志在事务中变化")
            return "prepared"

        result[await audited_mutations.mutation_transaction(repo, commit)] += 1
    return {"token": token, "nodeId": node_id, "candidates": len(candidates), **dict(result)}


async def complete_deployment_recovery(repo: Any, node_id: str, token: str) -> dict[str, Any]:
    """在容器停止已由外部确认后写关闭证明，并仅释放仍可重新调度的原 owner。"""
    if not _TOKEN.fullmatch(token):
        raise ValueError("部署恢复令牌格式无效")
    result: Counter[str] = Counter()
    cursor = repo.db.runs.find({"deploymentRecovery.token": token, "deploymentRecovery.nodeId": node_id})
    markers = [run async for run in cursor]
    for marker in markers:
        outcome = await _complete_run(repo, node_id, token, marker)
        result.update(outcome)
    return {"token": token, "nodeId": node_id, "preparedRuns": len(markers), **dict(result)}


async def _complete_run(repo: Any, node_id: str, token: str, marker: dict[str, Any]) -> Counter[str]:
    """原子完成单个 run；证明写入和任务释放分别判断，避免用户控制意图被覆盖。"""
    prepared = marker.get("deploymentRecovery") or {}
    identity = {key: prepared.get(key) for key in _IDENTITY}
    if identity.get("nodeId") != node_id or any(identity.get(key) is None for key in _IDENTITY):
        return Counter(invalidMarker=1)

    async def commit(session):
        """先固化 immutable proof，再按当前 task 控制状态决定是否允许释放。"""
        current_run = await repo.db.runs.find_one(_run_owner_filter(identity) | {"deploymentRecovery.token": token},
                                                  session=session)
        if current_run is None:
            return Counter(runChanged=1)
        task = await repo.db.tasks.find_one(_owner_filter(identity) | {"deploymentRecovery.token": token}, session=session)
        foreign = await repo.db.endpoint_locks.find_one(
            {"taskId": identity["taskId"], "runId": {"$ne": identity["runId"]}}, session=session,
        )
        if task is not None and foreign is not None:
            # 未消费 marker 即中止整笔事务；旧容器已经停止时可由同 token 重试，不留下死 owner。
            raise RuntimeError("部署恢复发现后继运行锁")
        # 优雅退出可能已经清除节点归属。仍指向同一运行的暂停意图必须保留预算；
        # 已迁移的新运行只用于识别竞争，绝不能修改它的任务或端点锁。
        current_task = task or await repo.db.tasks.find_one({"id": identity["taskId"]}, session=session)
        preserve_paused_run = bool(current_task and current_task.get("runId") == identity["runId"]
                                   and current_task.get("desiredState") == "PAUSED")
        control_matches = (
            task is not None and task.get("desiredState") == "RUNNING"
            and task.get("controlClaimVersion") == prepared.get("controlClaimVersion")
            and task.get("controlOperationId") == prepared.get("controlOperationId")
            and task.get("resourceDeleted") is not True
        )
        proof = {
            "token": token, "nodeId": node_id, "taskId": identity["taskId"], "runId": identity["runId"],
            "generation": identity["generation"], "sessionId": identity["sessionId"],
            "confirmedAt": now().isoformat(),
        }
        changed = await repo.db.runs.update_one(
            _run_owner_filter(identity) | {"deploymentRecovery.token": token, "$or": [
                {"sessionId": {"$exists": False}}, {"sessionId": None}, {"sessionId": identity["sessionId"]},
            ]},
            {"$set": {"sessionId": identity["sessionId"], "deploymentClosedProof": proof,
                      **({} if preserve_paused_run else {"endedAt": now(), "endReason": "CONTROLLED_DEPLOYMENT"})},
             "$unset": {"deploymentRecovery": ""}},
            session=session,
        )
        if not changed.matched_count:
            raise RuntimeError("部署关闭证明在事务中变化")
        files = [item async for item in repo.db.files.find({
            "deploymentRecovery.token": token, "deploymentRecovery.nodeId": node_id,
            "deploymentRecovery.taskId": identity["taskId"], "deploymentRecovery.runId": identity["runId"],
            "deploymentRecovery.generation": identity["generation"], "status": "OPEN",
        }, session=session)]
        for file in files:
            marker = file.get("deploymentRecovery") or {}
            file_proof = proof | {"fileId": marker.get("fileId"), "path": marker.get("path"),
                                  "sessionId": marker.get("sessionId"), "createdAt": marker.get("createdAt"),
                                  "watermark": marker.get("watermark"),
                                  "firstSequence": marker.get("firstSequence")}
            changed = await repo.db.files.update_one(
                {"id": marker.get("fileId"), "path": marker.get("path"), "sessionId": marker.get("sessionId"),
                 "status": "OPEN", "deploymentRecovery.token": token},
                {"$set": {"deploymentClosedProof": file_proof}, "$unset": {"deploymentRecovery": ""}},
                session=session,
            )
            if not changed.matched_count:
                raise RuntimeError("部署关闭原始日志在事务中变化")
        outcome = Counter(proved=1)
        if task is None:
            # 旧 Worker 优雅关闭可能先释放 task owner；冻结文件仍是已停止容器留下的
            # 唯一可验证原始日志，必须完成 proof，但不能触碰后继任务或其锁。
            return outcome | Counter(taskChanged=1)
        if not control_matches:
            if task.get("protocol") == "SSH":
                from camera_logs.collection.ssh_admission import release_task_slots
                await release_task_slots(repo, task, session=session)
            if task.get("desiredState") != "PAUSED":
                for old, new in (("SENDING", "UNKNOWN"), ("QUEUED", "CANCELLED")):
                    await repo.db.commands.update_many(
                        {"taskId": identity["taskId"], "runId": identity["runId"], "status": old},
                        {"$set": {"status": new, "completedAt": now()}}, session=session,
                    )
            changed = await repo.db.tasks.update_one(
                _owner_filter(identity) | {"deploymentRecovery.token": token},
                {"$set": {"nodeId": None, "status": "PAUSED" if task.get("desiredState") == "PAUSED" else "STOPPED",
                          "updatedAt": now()}, "$unset": {"deploymentRecovery": ""}}, session=session,
            )
            if not changed.matched_count:
                raise RuntimeError("部署恢复控制状态在事务中变化")
            await repo.db.endpoint_locks.delete_one(
                {"taskId": identity["taskId"], "runId": identity["runId"]}, session=session,
            )
            return outcome | Counter(controlChanged=1)
        if task.get("protocol") == "SSH":
            from camera_logs.collection.ssh_admission import release_task_slots
            await release_task_slots(repo, task, session=session)
        for old, new in (("SENDING", "UNKNOWN"), ("QUEUED", "CANCELLED")):
            await repo.db.commands.update_many(
                {"taskId": identity["taskId"], "runId": identity["runId"], "status": old},
                {"$set": {"status": new, "completedAt": now()}}, session=session,
            )
        changed = await repo.db.tasks.update_one(
            _owner_filter(identity) | {"deploymentRecovery.token": token, "desiredState": "RUNNING",
                                       "controlClaimVersion": prepared.get("controlClaimVersion"),
                                       "controlOperationId": prepared.get("controlOperationId"),
                                       "resourceDeleted": {"$ne": True}},
            {"$set": {"nodeId": None, "status": "STOPPED", "error": None, "restartRequested": False,
                      "updatedAt": now(), "recoveryReason": "CONTROLLED_DEPLOYMENT"},
             "$unset": {"deploymentRecovery": "", "plannedRestart": "", "plannedRestartNodeId": "",
                        "plannedRestartToken": "", "closedReceipt": ""}}, session=session,
        )
        if not changed.matched_count:
            # 此前已在本事务调整 SSH 名额和命令状态，CAS 丢失必须整体回滚，不能半收尾。
            raise RuntimeError("部署恢复任务归属在事务中变化")
        await repo.db.endpoint_locks.delete_one(
            {"taskId": identity["taskId"], "runId": identity["runId"]}, session=session,
        )
        return outcome | Counter(released=1)

    return await audited_mutations.mutation_transaction(repo, commit)


async def _main(options) -> None:
    """建立一次性 Mongo 连接，执行指定阶段并以固定 JSON 向部署脚本报告结果。"""
    settings = Settings()
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                              w="majority", journal=True)
    try:
        repo = Repository(client[settings.database_name], settings)
        await repo.initialize()
        result = await (prepare_deployment_recovery(repo, settings.node_id) if options.phase == "prepare"
                        else complete_deployment_recovery(repo, settings.node_id, options.token))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        await client.close()


def main() -> None:
    """解析一次性部署恢复命令；complete 必须显式带 prepare 返回的令牌。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("prepare", "complete"))
    parser.add_argument("--token")
    options = parser.parse_args()
    if options.phase == "complete" and not options.token:
        parser.error("complete 必须提供 --token")
    asyncio.run(_main(options))


if __name__ == "__main__":
    main()
