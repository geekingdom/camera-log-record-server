"""本地及跨节点全文候选收集；未知证据保守回退，不改变原文检索合同。"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from pathlib import Path

import httpx

from camera_logs.logs.job_threads import job_thread
from camera_logs.logs.search_index import query_index
from camera_logs.logs.search_plan import plan_candidate_files

logger = logging.getLogger(__name__)


def contained_archive(repo, file):
    """只接受当前 Worker 根目录内的正式小时压缩包。"""
    root = Path(repo.settings.log_root).resolve()
    path = Path(file.get("path") or "")
    path = (path if path.is_absolute() else root / path).resolve()
    if root not in path.parents or path.suffixes[-2:] != [".tar", ".gz"]:
        return None
    return path


async def local_index_evidence(repo, files, keyword, *, cancelled=None):
    """每个本地小时包只查询一次索引；成员、水位或节点不符时不提供阴性证明。"""
    grouped = defaultdict(list)
    for file in files:
        if file.get("status") != "READY" or file.get("nodeId") != repo.settings.node_id:
            continue
        path = contained_archive(repo, file)
        if path is not None:
            grouped[path].append(file)
    evidence = {}
    for path, members in grouped.items():
        if cancelled is not None and await cancelled():
            raise asyncio.CancelledError()
        indexed = await job_thread(query_index, path, keyword)
        if indexed is None:
            continue
        for file in members:
            item = indexed.get(file.get("archiveMember"))
            if item is not None and item.get("bytes") == file.get("bytes"):
                evidence[file["id"]] = item
    return evidence


async def search_candidates(repo, frozen_files, keyword, *, cancelled=None):
    """按节点批量获取索引证据；RPC失败仅降低速度，不降低结果完整性。"""
    if len(keyword) < 3 or any(character in keyword for character in "\r\n\0"):
        return {file["id"] for file in frozen_files}
    from camera_logs.indexing.query import query_evidence
    evidence, _index_status = await query_evidence(repo.settings, frozen_files, keyword)
    remote = defaultdict(list)
    # 以当前目录定位已发布归档，但只将成员/水位与冻结清单一致者用于排除。
    current_files = {}
    for start in range(0, len(frozen_files), 256):
        if cancelled is not None and await cancelled():
            raise asyncio.CancelledError()
        identifiers = [file["id"] for file in frozen_files[start:start + 256]]
        async for file in repo.db.files.find({"id": {"$in": identifiers}, "status": "READY"}):
            current_files[file["id"]] = file
    local = []
    for frozen in frozen_files:
        if frozen["id"] in evidence:
            continue
        current = current_files.get(frozen["id"])
        if (frozen.get("status") != "READY" or current is None
                or any(current.get(key) != frozen.get(key) for key in ("bytes", "archiveMember", "nodeId"))):
            continue
        if current.get("nodeId") == repo.settings.node_id:
            local.append(current)
        else:
            remote[current.get("nodeId")].append(current)
    evidence.update(await local_index_evidence(repo, local, keyword, cancelled=cancelled))
    async with httpx.AsyncClient(timeout=10) as client:
        for node_id, files in remote.items():
            node = await repo.db.nodes.find_one({"id": node_id})
            if not node:
                continue
            for start in range(0, len(files), 256):
                if cancelled is not None and await cancelled():
                    raise asyncio.CancelledError()
                chunk = files[start:start + 256]
                allowed = {file["id"] for file in chunk}
                try:
                    response = await client.post(node["url"] + "/internal/search-index/candidates",
                        json={"fileIds": list(allowed), "keyword": keyword},
                        headers={"Authorization": "Bearer " + repo.settings.internal_token})
                    response.raise_for_status()
                    returned = response.json()["items"]
                    if not isinstance(returned, list) or len(returned) > len(chunk):
                        raise ValueError("候选索引响应数量无效")
                    for item in returned:
                        if not isinstance(item, dict):
                            raise TypeError("候选索引响应格式无效")
                        if item.get("fileId") not in allowed:
                            continue
                        if any(not isinstance(item.get(key), str) or len(item[key]) > 32 for key in ("headHex", "tailHex")):
                            raise ValueError("候选边界长度无效")
                        head, tail = bytes.fromhex(item["headHex"]), bytes.fromhex(item["tailHex"])
                        if len(head) <= 16 and len(tail) <= 16:
                            evidence[item["fileId"]] = item | {"head": head, "tail": tail}
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    logger.warning("远端全文候选不可用，回退精确原文扫描 node=%s", node_id)
                await asyncio.sleep(0)
    return plan_candidate_files(frozen_files, evidence, keyword)
