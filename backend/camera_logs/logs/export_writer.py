"""下载导出写入归属与关闭证据。

作业执行者在创建目录前登记自身 token，完成所有文件线程后才写入关闭证据；
维护回收据此与内核锁共同排除仍可能写入的旧执行者。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def _writer_filter(job: dict[str, Any]) -> dict[str, Any]:
    """限定本次执行者登记或关闭产物，旧 token 不能伪造写入结束证据。"""
    condition: dict[str, Any] = {"id": job["id"], "status": "RUNNING"}
    if job.get("executionToken"):
        condition.update(executionToken=job["executionToken"], nodeId=job["nodeId"])
    else:
        condition["executionToken"] = {"$exists": False}
    return condition


async def _begin_download_output(repo: Any, job: dict[str, Any]) -> bool:
    """在创建目录前登记本节点写入归属，维护器据此拒绝未关闭产物。"""
    changed = await repo.db.jobs.update_one(
        _writer_filter(job),
        {"$set": {"outputExecutionState": "WRITING", "outputWriterNodeId": repo.settings.node_id,
                  "outputWriterToken": job.get("executionToken"), "outputWriterStartedAt": datetime.now(UTC),
                  "outputLockProtocol": 1, "outputCleanupState": "IDLE", "outputReaders": []}},
    )
    return changed.matched_count == 1


async def _close_download_output(repo: Any, job: dict[str, Any]) -> None:
    """文件线程均已退出后关闭写入证据，允许受限维护回收终态产物。"""
    condition = {
        "id": job["id"], "outputExecutionState": "WRITING",
        "outputWriterNodeId": repo.settings.node_id,
        "outputWriterToken": job.get("executionToken"),
    }
    await repo.db.jobs.update_one(
        condition,
        {"$set": {"outputExecutionState": "CLOSED", "outputWriterClosedAt": datetime.now(UTC)}},
    )
