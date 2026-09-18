"""搜索运行中的有界结果发布，确保旧执行器不能覆盖现有作业。"""

from __future__ import annotations

from datetime import UTC, datetime


def _running_filter(job):
    """生成与终态提交一致的执行归属条件，兼容尚无租约 token 的历史作业。"""
    query = {"id": job["id"], "status": "RUNNING"}
    if job.get("executionToken"):
        query.update(
            executionToken=job["executionToken"], nodeId=job["nodeId"],
            leaseUntil={"$gt": datetime.now(UTC)},
        )
    else:
        # Mongo 的 ``null`` 查询同时匹配旧记录缺少字段的情形；与终态 CAS 的
        # 历史作业兼容分支一致，且仍拒绝已被新执行 token 领取的作业。
        query["executionToken"] = None
    return query


async def publish_search_results(repo, job, results):
    """原子发布完整的已确认前缀；返回 false 表示作业已取消或失去执行权。"""
    if not results:
        return True
    query = _running_filter(job)
    changed = await repo.db.jobs.update_one(
        query,
        {"$set": {"results": results, "resultCount": len(results)}},
    )
    # 相同前缀的幂等重试可匹配但不修改；这仍证明当前执行器拥有作业，不能被
    # MongoMock 的可变列表引用或数据库重试误判为租约丢失。
    return bool(changed.matched_count)
