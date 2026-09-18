"""每轮有界补建旧归档全文索引，使用持久游标避免反复只处理列表头。"""

import logging

from camera_logs.logs.compression import build_search_index
from camera_logs.logs.job_threads import job_thread
from camera_logs.logs.search_candidates import contained_archive
from camera_logs.logs.search_index import query_index

logger = logging.getLogger(__name__)


async def backfill_search_indexes(repo):
    """每分钟最多查看64个目录项、补建一个小时包；失败推进游标后下轮完整巡回重试。"""
    node = await repo.db.nodes.find_one({"id": repo.settings.node_id}) or {}
    query = {"nodeId": repo.settings.node_id, "status": "READY"}
    if node.get("searchIndexCursor"):
        query["id"] = {"$gt": node["searchIndexCursor"]}
    files = [file async for file in repo.db.files.find(query).sort("id", 1).limit(64)]
    if not files:
        await repo.db.nodes.update_one({"id": repo.settings.node_id}, {"$unset": {"searchIndexCursor": ""}})
        return 0
    visited, built = set(), 0
    for file in files:
        await repo.db.nodes.update_one({"id": repo.settings.node_id}, {"$set": {"searchIndexCursor": file["id"]}})
        path = contained_archive(repo, file)
        if path is None or path in visited:
            continue
        visited.add(path)
        if await job_thread(query_index, path, "__index_health_probe__") is not None:
            continue
        try:
            built = int(await build_search_index(path))
            if not built:
                logger.warning("全文索引暂不可构建，继续使用原文扫描 node=%s", repo.settings.node_id)
        except OSError:
            logger.exception("旧归档全文索引补建失败 node=%s", repo.settings.node_id)
        break
    return built
