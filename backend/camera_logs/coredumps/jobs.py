"""节点执行 coredump 异步导出：先冻结，再从各节点流式汇聚 ZIP STORE。"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from pymongo.errors import DuplicateKeyError

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.coredumps.snapshots import freeze, write_zip
from camera_logs.logs.archive_access import copy_limited
from camera_logs.logs.job_threads import job_thread
from camera_logs.logs.naming import safe_filename_component

_io_slots = asyncio.Semaphore(2)


async def _cancelled(repo: Any, job: dict[str, Any]) -> bool:
    """每个文件阶段重新读取取消状态，避免取消后继续创建 ZIP 或发布产物。"""
    current = await repo.db.coredump_exports.find_one({"id": job["id"], "workerInstanceId": job.get("workerInstanceId")}, {"status": 1})
    if current and current.get("status") == "RUNNING":
        renewed = await repo.db.coredump_exports.update_one({"id": job["id"], "status": "RUNNING",
            "workerInstanceId": job.get("workerInstanceId")}, {"$set": {"leaseUntil": now() + timedelta(seconds=90)}})
        return renewed.matched_count != 1
    return True


async def _reserve(repo: Any, job: dict[str, Any]) -> int:
    """创建每作业幂等容量声明，并和节点总用量在同一事务中原子增加。"""
    # ZIP 本体外还会写本地文件头、中央目录和结束记录；按每成员 4KiB 和固定
    # 64KiB 保守估计，不能让大量小文件绕过节点空间额度。
    amount = int(job.get("estimatedBytes", 0)) * 2 + len(job.get("sources", [])) * 4096 + 65536
    if amount > int(repo.settings.coredump_export_quota_bytes):
        raise OverflowError("coredump 导出节点配额不足")
    collection, node_id = repo.db.coredump_export_reservations, repo.settings.node_id
    claims = repo.db.coredump_export_reservation_claims

    async def claim(session):
        existing = await claims.find_one({"id": job["id"]}, session=session)
        if existing:
            if existing.get("nodeId") != node_id or existing.get("state") != "RESERVED":
                raise RuntimeError("coredump 导出容量声明不可用")
            return int(existing["bytes"])
        await collection.update_one({"id": node_id}, {"$setOnInsert": {"used": 0, "updatedAt": now()}}, upsert=True, session=session)
        changed = await collection.update_one({"id": node_id, "used": {"$lte": int(repo.settings.coredump_export_quota_bytes) - amount}},
                                              {"$inc": {"used": amount}, "$set": {"updatedAt": now()}}, session=session)
        if changed.matched_count != 1:
            raise OverflowError("coredump 导出节点配额不足")
        await claims.insert_one({"id": job["id"], "nodeId": node_id, "bytes": amount,
                                 "state": "RESERVED", "createdAt": now()}, session=session)
        return amount
    try:
        return await audited_mutations.mutation_transaction(repo, claim)
    except DuplicateKeyError:
        # 唯一 id 的并发提交中，失败方只读复用已经确认的同一作业声明。
        existing = await claims.find_one({"id": job["id"], "nodeId": node_id, "state": "RESERVED"})
        if existing:
            return int(existing["bytes"])
        raise


async def _release(repo: Any, job_id: str) -> None:
    """只把 RESERVED claim 转为 RELEASED 一次，重复清理不能令节点用量为负。"""
    claims, totals = repo.db.coredump_export_reservation_claims, repo.db.coredump_export_reservations

    async def release(session):
        claim = await claims.find_one({"id": job_id, "state": "RESERVED"}, session=session)
        if not claim:
            return False
        changed = await claims.update_one({"id": job_id, "state": "RESERVED"}, {"$set": {"state": "RELEASED", "releasedAt": now()}}, session=session)
        if changed.matched_count != 1:
            return False
        result = await totals.update_one({"id": claim["nodeId"], "used": {"$gte": int(claim["bytes"])}},
                                         {"$inc": {"used": -int(claim["bytes"])}, "$set": {"updatedAt": now()}}, session=session)
        if result.matched_count != 1:
            raise RuntimeError("coredump 导出配额账本损坏")
        return True
    return await audited_mutations.mutation_transaction(repo, release)


async def _complete(repo: Any, job: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """只重试终态数据库事务；审计仅在 RUNNING 实际转换时写入。"""
    delay = 1
    while True:
        async def commit(session):
            current = await repo.db.coredump_exports.find_one({"id": job["id"]}, session=session)
            if current is None:
                return {"status": "CANCELLED", "_transitioned": False}
            if current.get("status") != "RUNNING":
                return {"status": current.get("status", "CANCELLED"), "_transitioned": False}
            changed = await repo.db.coredump_exports.update_one(
                {"id": job["id"], "status": "RUNNING", "workerInstanceId": job.get("workerInstanceId")}, {"$set": update | {"updatedAt": now()}}, session=session)
            if changed.matched_count != 1:
                return {"status": "CANCELLED", "_transitioned": False}
            await repo.audit(job["actor"], "coredump_export_" + update["status"].lower(), job["id"], session=session)
            return {"status": update["status"], "_transitioned": True}
        try:
            return await audited_mutations.mutation_transaction(repo, commit)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 数据库提交异常仅影响确认，不得重做文件产物。
            if asyncio.current_task().cancelling():
                raise asyncio.CancelledError()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


async def cleanup_expired_exports(repo: Any) -> int:
    """以 RESERVED claim 回收崩溃遗留的 scratch/output，物理清理后才释放额度。"""
    root = Path(repo.settings.log_root).resolve() / "exports" / "coredumps"
    scratch_root = root / ".tmp"
    removed = 0
    claims = repo.db.coredump_export_reservation_claims.find({"nodeId": repo.settings.node_id, "state": "RESERVED"})
    async for claim in claims:
        identifier = claim["id"]
        if not isinstance(identifier, str) or Path(identifier).name != identifier:
            continue
        document = await repo.db.coredump_exports.find_one({"id": identifier})
        expires = document.get("expiresAt") if document else None
        active = bool(document and document.get("status") in {"QUEUED", "RUNNING"}) or bool(document and document.get("status") == "SUCCEEDED" and expires and
                      (expires.replace(tzinfo=UTC) if expires.tzinfo is None else expires.astimezone(UTC)) > datetime.now(UTC))
        if not active:
            # 目录名只能来自已登记 UUID；仍限定到受控 roots，不能使用数据库路径字段。
            await job_thread(_remove_directory, scratch_root / identifier)
            await job_thread(_remove_directory, root / identifier)
            await _release(repo, identifier)
            removed += 1
    return removed


def _remove_directory(path: Path) -> None:
    """缺失目录视为已清理，其他删除错误必须保留配额以便后续重试。"""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return


def _digest(path: Path) -> str:
    """在工作线程中读取完整文件摘要，文件描述符随上下文关闭。"""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


async def _freeze(repo: Any, file: dict[str, Any]) -> dict[str, Any]:
    """本节点直接冻结，远端通过内部令牌触发；始终返回数据库确认后的版本。"""
    if file["nodeId"] == repo.settings.node_id:
        return await freeze(repo, file)
    node = await repo.get("nodes", file["nodeId"])
    async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=None, write=30, pool=30)) as client:
        response = await client.post(node["url"] + f"/internal/coredumps/{file['id']}/freeze",
            headers={"Authorization": "Bearer " + repo.settings.internal_token})
    if response.status_code == 413:
        raise OverflowError("coredump 超过节点快照上限")
    if response.status_code != 200:
        raise RuntimeError("远端 coredump 未能冻结")
    result = await repo.db.coredump_files.find_one({"id": file["id"], "status": "FROZEN"})
    if not result:
        raise RuntimeError("远端冻结结果未同步")
    return result


async def _fetch(repo: Any, file: dict[str, Any], target: Path, limit: int) -> Path:
    """仅按固定副本内部接口跨节点取回，逐块写盘并限制总导出大小。"""
    if file["nodeId"] == repo.settings.node_id:
        source = Path(file["snapshot"]["path"])
        await job_thread(copy_limited, source, target, max_output_bytes=limit)
        digest = await job_thread(_digest, target)
        if digest != file["snapshot"]["sha256"]:
            target.unlink(missing_ok=True)
            raise RuntimeError("本地冻结副本摘要不匹配")
        return target
    node = await repo.get("nodes", file["nodeId"])
    written, digest = 0, hashlib.sha256()
    async with httpx.AsyncClient(timeout=300) as client, client.stream("GET", node["url"] + f"/internal/coredumps/{file['id']}/content",
        headers={"Authorization": "Bearer " + repo.settings.internal_token}) as response:
        response.raise_for_status()
        with target.open("xb") as output:
            async for chunk in response.aiter_bytes(1024 * 1024):
                written += len(chunk)
                if written > limit:
                    raise OverflowError("coredump 导出超过产物上限")
                digest.update(chunk)
                await _write(output, chunk)
    if written != int(file["snapshot"]["size"]) or f'"{digest.hexdigest()}"' != file["snapshot"]["etag"]:
        target.unlink(missing_ok=True)
        raise RuntimeError("远端冻结副本长度或摘要不匹配")
    return target


async def _write(output, data: bytes) -> None:
    """跨节点下载写入使用有界线程槽，不能在事件循环中阻塞大文件落盘。"""
    async with _io_slots:
        await job_thread(output.write, data)


def _member_name(file: dict[str, Any], index: int) -> str:
    """ZIP 成员以节点、资源和 ID 消歧，源目录层级和绝对路径都不进入产物。"""
    base = safe_filename_component(Path(file["name"]).name, fallback=file["id"])
    return f"{safe_filename_component(file['nodeId'], fallback='node')}/{safe_filename_component(file['resourceId'], fallback='resource')}/{index + 1}-{file['id'][:12]}-{base}"


async def run_export(repo: Any, job: dict[str, Any]) -> dict[str, Any]:
    """作业顺序冻结全部版本，再生成单副本或跨节点 ZIP；取消后删除未发布产物。"""
    root = Path(repo.settings.log_root).resolve() / "exports" / "coredumps"
    scratch, output = root / ".tmp" / job["id"], root / job["id"]
    limit = int(repo.settings.coredump_export_max_bytes)
    reserved = 0
    heartbeat = None
    try:
        reserved = await _reserve(repo, job)
        async def renew_lease():
            while True:
                await asyncio.sleep(20)
                if await _cancelled(repo, job):
                    return
        heartbeat = asyncio.create_task(renew_lease())
        await job_thread(scratch.mkdir, parents=True, exist_ok=True)
        frozen = []
        for source in job["sources"]:
            if await _cancelled(repo, job):
                raise asyncio.CancelledError()
            file = await repo.get("coredump_files", source["id"])
            if any(file.get(key) != source.get(key) for key in ("nodeId", "resourceId", "version", "source")):
                raise RuntimeError("coredump 目录版本已变化")
            frozen.append(await _freeze(repo, file))
        staged = []
        for index, file in enumerate(frozen):
            if await _cancelled(repo, job):
                raise asyncio.CancelledError()
            staged.append((file["name"], await _fetch(repo, file, scratch / f"{index}.core", limit)))
        if await _cancelled(repo, job):
            raise asyncio.CancelledError()
        await job_thread(output.mkdir, parents=True, exist_ok=True)
        if len(staged) == 1:
            _name, source = staged[0]; result = output / safe_filename_component(Path(frozen[0]["name"]).name, fallback=frozen[0]["id"])
            await job_thread(copy_limited, source, result, max_output_bytes=limit)
        else:
            result = output / "coredumps.zip"
            members = [(_member_name(file, index), path) for index, (file, (_name, path)) in enumerate(zip(frozen, staged))]
            await job_thread(write_zip, result, members, limit)
        if await _cancelled(repo, job):
            raise asyncio.CancelledError()
        etag = await job_thread(_digest, result)
        update = {"status": "SUCCEEDED", "resultPath": str(result), "filename": result.name, "reservedBytes": reserved,
                  "bytes": result.stat().st_size, "etag": f'"{etag}"'}
    except asyncio.CancelledError:
        update = {"status": "CANCELLED"}
    except Exception as error:  # noqa: BLE001 - 文件、节点和压缩错误统一持久化为作业失败。
        update = {"status": "FAILED", "error": type(error).__name__}
    finally:
        if heartbeat:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        await job_thread(_remove_directory, scratch)
        if "update" in locals() and update["status"] != "SUCCEEDED":
            await job_thread(_remove_directory, output)
            await _release(repo, job["id"])
    actual = await _complete(repo, job, update)
    if actual["status"] == "CANCELLED" and update["status"] == "SUCCEEDED":
        # 事务已确认取消，才在事务外回收尚未发布的成功产物。
        await job_thread(_remove_directory, output)
        await _release(repo, job["id"])
    elif actual["status"] != "SUCCEEDED" and update["status"] == "SUCCEEDED":
        await job_thread(_remove_directory, output)
        await _release(repo, job["id"])
    return {key: value for key, value in actual.items() if key != "_transitioned"}
