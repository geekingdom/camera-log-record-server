"""本节点归档保留期维护及文件目录恢复。

只清理已校验发布、超过保留期且未被导出保护的本地文件；数据库先声明删除
状态，避免新导出和清理竞争。节点重启时扫描已发布归档修复目录登记。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pymongo import ReturnDocument

from camera_logs.common.database import now
from camera_logs.common.presentation_time import utc_iso
from camera_logs.logs.archive_spool import load as load_archive_spool
from camera_logs.logs.archive_spool import persist as persist_archive_spool
from camera_logs.logs.catalog_identity import file_identity
from camera_logs.logs.compression import _hash_stream, schedule_pending_archive, schedule_recovery_archive
from camera_logs.logs.export_locks import ExportLock
from camera_logs.logs.job_threads import job_thread
from camera_logs.logs.naming import segment_name_from_source
from camera_logs.logs.search_index import index_path

logger = logging.getLogger(__name__)
EXPORT_CLEANUP_BATCH_SIZE = 100


def _root(repo: Any) -> Path:
    return Path(repo.settings.log_root).resolve()


def _contained(repo: Any, path: str | Path) -> Path:
    value = Path(path)
    value = value.resolve() if value.is_absolute() else (_root(repo) / value).resolve()
    if _root(repo) not in value.parents:
        raise ValueError("archive path is outside log root")
    return value


def _archive_id(root: Path, path: Path, node_id: str | None = None) -> str:
    """根据原始日志相对路径生成稳定目录 ID，与运行时登记规则一致。"""
    return file_identity(root, path, node_id)


def _metadata_path(path: Path) -> Path:
    """共享小时归档的成员清单固定放在同目录旁路文件中。"""
    return Path(str(path) + ".metadata.json")


def _pending_path(path: Path) -> Path:
    """压缩事务清单存在时，归档尚未完成 metadata 发布和原始分卷清理。"""
    return Path(str(path) + ".pending.json")


def _remove_empty_log_ancestors(root: Path, archive: Path) -> None:
    """在已删归档的原目录向上 rmdir 空目录，绝不越过日志根或穿过软链接。"""
    root = Path(root).absolute()
    archive = Path(archive)
    archive = archive if archive.is_absolute() else root / archive
    try:
        archive.relative_to(root)
    except ValueError:
        return
    if root.is_symlink() or not root.is_dir():
        return
    ancestors: list[Path] = []
    current = archive.parent
    while current != root:
        if current.is_symlink():
            return
        ancestors.append(current)
        parent = current.parent
        if parent == current:
            return
        current = parent
    for directory in ancestors:
        try:
            directory.rmdir()
        except OSError:
            return


def _pending_segments(path: Path) -> list[dict[str, Any]] | None:
    """读取待提交清单；格式损坏交由压缩器拒绝，维护不把它误作已完成归档。"""
    pending = _pending_path(path)
    if not pending.is_file() or pending.stat().st_size > 1024 * 1024:
        return None
    value = json.loads(pending.read_text(encoding="utf-8"))
    segments = value.get("segments") if isinstance(value, dict) else None
    return segments if isinstance(value, dict) and value.get("formatVersion") == 1 and isinstance(segments, list) and segments else None


async def _pending_is_active(repo: Any, segments: list[dict[str, Any]]) -> bool:
    """当前节点仍使用该任务和会话时，让其自身压缩流程完成事务，避免并行改写。"""
    for segment in segments:
        current = await repo.db.tasks.find_one({
            "id": segment.get("taskId"), "runId": segment.get("runId"), "sessionId": segment.get("sessionId"),
            "nodeId": repo.settings.node_id, "status": {"$in": ["CONNECTING", "COLLECTING", "RECONNECTING", "PAUSING"]},
        })
        if current is not None:
            return True
    return False


def _proof_identity(run: dict[str, Any]) -> dict[str, Any] | None:
    """只接受部署事务已提交的完整停机证明，拒绝心跳或普通收尾记录替代。"""
    proof = run.get("deploymentClosedProof")
    keys = ("token", "nodeId", "taskId", "runId", "generation", "sessionId", "confirmedAt")
    if not isinstance(proof, dict) or any(proof.get(key) in (None, "") for key in keys):
        return None
    if not isinstance(proof["token"], str) or not isinstance(proof["nodeId"], str):
        return None
    if proof["taskId"] != run.get("taskId") or proof["runId"] != run.get("id"):
        return None
    # run 文档是部署 complete CAS 固化的旧 owner 快照；缺少任何一项时不得
    # 由当前 task 的新归属反推，以免在迁移后的新会话旁路归档。
    if any(proof[key] != run.get(key) for key in ("nodeId", "generation", "sessionId")):
        return None
    try:
        datetime.fromisoformat(str(proof["confirmedAt"]))
    except ValueError:
        return None
    return {
        key: proof[key]
        for key in ("token", "taskId", "runId", "sessionId", "nodeId", "generation")
    }


def _raw_recovery_target(log: Path) -> Path:
    """按旧分卷文件名恢复所属小时包名，仍优先复用目录内已经存在的共享包。"""
    existing = sorted(log.parent.glob("*.tar.gz"))
    if existing:
        return existing[0]
    match = re.fullmatch(r"(.+)-part-\d+\.log", log.name)
    if match is None:
        raise ValueError("raw log filename has no part number")
    return log.parent / f"{match.group(1)}.tar.gz"


def _raw_hour(log: Path) -> datetime:
    """从 Writer 的上海日期/小时目录取得归档小时，拒绝不符合固定目录的文件。"""
    date, hour = log.parent.parent.name, log.parent.name
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", date)
    if match is None or not re.fullmatch(r"\d{2}", hour):
        raise ValueError("raw log path has no writer hour")
    from zoneinfo import ZoneInfo
    return datetime(*(int(value) for value in match.groups()), int(hour), tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(UTC)


def _raw_segment(log: Path, identity: dict[str, Any]) -> dict[str, Any]:
    """逐行验证旧索引与正文边界后重建一个可由现有发布器消费的 manifest。"""
    index = log.with_suffix(".index.jsonl")
    if not index.is_file() or log.stat().st_size <= 0:
        raise ValueError("raw log or index is missing")
    offset = previous_sequence = None
    first_sequence = last_sequence = None
    first_received_at = last_received_at = None
    received_times_reliable = True
    with index.open(encoding="utf-8") as source:
        for line in source:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise TypeError("index row is not an object")
            sequence, row_offset, length, received_at = (record.get(key) for key in
                ("sequence", "offset", "length", "receivedAt"))
            if (not isinstance(sequence, int) or isinstance(sequence, bool) or not isinstance(row_offset, int)
                    or isinstance(row_offset, bool) or not isinstance(length, int) or isinstance(length, bool)
                    or length <= 0 or row_offset < 0):
                raise ValueError("index row has invalid position")
            received = datetime.fromisoformat(str(received_at))
            # 旧索引曾允许无时区字符串。它不能证明服务器实际接收时刻，恢复时
            # 只能保留正文和顺序，成员名明确使用 unknown-time，绝不按本机时区猜测。
            if received.tzinfo is None or received.utcoffset() is None:
                received = None
                received_times_reliable = False
            if offset is None and row_offset != 0:
                raise ValueError("index does not start at raw log head")
            if offset is not None and row_offset != offset:
                raise ValueError("index offset is not continuous")
            if previous_sequence is not None and sequence != previous_sequence + 1:
                raise ValueError("index sequence is not continuous")
            offset, previous_sequence = row_offset + length, sequence
            first_sequence = sequence if first_sequence is None else first_sequence
            last_sequence = sequence
            if received is not None:
                first_received_at = received if first_received_at is None else first_received_at
                last_received_at = received
    raw_size = log.stat().st_size
    if offset != raw_size or first_sequence is None or last_sequence is None:
        raise ValueError("index does not cover raw log exactly")
    with log.open("rb") as source:
        digest, verified_size = _hash_stream(source)
    with index.open("rb") as source:
        index_digest, index_size = _hash_stream(source)
    if verified_size != raw_size:
        raise ValueError("raw log size changed during recovery")
    if not received_times_reliable:
        first_received_at = last_received_at = None
    source_name = log.name
    return {"taskId": identity["taskId"], "runId": identity["runId"], "sessionId": identity["sessionId"],
            "hourStart": _raw_hour(log).isoformat(), "rawSize": raw_size, "sha256": digest,
            "firstSequence": first_sequence, "lastSequence": last_sequence, "logPath": str(log),
            "sourceLogName": source_name,
            "logName": segment_name_from_source(source_name, first_received_at, last_received_at),
            "indexName": index.name, "indexSha256": index_digest, "indexBytes": index_size,
            "firstReceivedAt": first_received_at.astimezone(UTC).isoformat() if first_received_at else None,
            "lastReceivedAt": last_received_at.astimezone(UTC).isoformat() if last_received_at else None}


async def _recover_proven_raw_segments(repo: Any, spooled_logs: set[Path]) -> None:
    """只按已验证部署停机证明定位旧 raw/index，持久化后交给有界调度器。"""
    cursor = repo.db.files.find({"deploymentClosedProof.nodeId": repo.settings.node_id,
                                 "deploymentClosedProof.token": {"$exists": True}, "status": "OPEN"})
    async for file in cursor:
        proof = file.get("deploymentClosedProof") or {}
        required = ("token", "nodeId", "taskId", "runId", "generation", "sessionId", "fileId", "path")
        if any(proof.get(key) in (None, "") for key in required) or any(
            proof.get(key) != file.get({"fileId": "id"}.get(key, key))
            for key in ("fileId", "nodeId", "taskId", "runId", "sessionId", "path")
        ):
            logger.warning("归档原始分卷未恢复 reason=invalid_deployment_file_proof file=%s", file.get("id"))
            continue
        identity = {key: proof[key] for key in ("taskId", "runId", "sessionId", "nodeId", "generation")}
        log = Path(proof["path"])
        try:
            resolved = log.resolve()
            if _root(repo) not in resolved.parents or resolved in spooled_logs:
                continue
            segment = await asyncio.to_thread(_raw_segment, resolved, identity)
            if await _pending_is_active(repo, [segment]):
                continue
            target = _raw_recovery_target(resolved)
            if not _pending_path(target).exists():
                spool = await asyncio.to_thread(persist_archive_spool, target, [segment])
                spooled_logs.add(resolved)
                schedule_pending_archive(spool)
            await repo.db.files.update_one(
                {"id": file["id"], "deploymentClosedProof.token": proof["token"]},
                {"$set": {"deploymentClosedProof.archiveRecoveredAt": now()}},
            )
        except Exception as error:  # noqa: BLE001 - 单卷证据异常不得阻塞其它旧运行恢复。
            logger.warning("归档原始分卷未恢复 reason=invalid_raw_index file=%s error=%s", file.get("id"), type(error).__name__)


def _member_path(archive: Path, name: Any, suffix: str) -> Path:
    """限制外部成员名称为当前小时目录的普通文件，阻止旁路元数据逃逸目录。"""
    if not isinstance(name, str) or not name.endswith(suffix) or Path(name).name != name:
        raise ValueError("invalid archive sidecar member name")
    return archive.parent / name


def _legacy_members(path: Path) -> list[dict[str, Any]]:
    """兼容旧单分卷 manifest，避免已发布历史归档在每轮维护中重复报错。"""
    with tarfile.open(path, "r:gz") as archive:
        manifest_file = archive.getmember("manifest.json")
        stream = archive.extractfile(manifest_file)
        if not manifest_file.isfile() or manifest_file.size > 1024 * 1024 or stream is None:
            raise ValueError("invalid legacy archive manifest")
        manifest = json.loads(stream.read())
        log = next((item for item in archive.getmembers() if item.isfile() and item.name.endswith(".log")), None)
        if log is None or not isinstance(manifest, dict):
            raise ValueError("invalid legacy archive log")
        source = archive.extractfile(log)
        if source is None or _hash_stream(source) != (manifest["sha256"], manifest["rawSize"]):
            raise ValueError("legacy archive checksum or size mismatch")
        index = next((item for item in archive.getmembers() if item.isfile() and item.name.endswith(".index.jsonl")), None)
        if index is not None:
            source = archive.extractfile(index)
            if source is None or _hash_stream(source) != (manifest["indexSha256"], manifest["indexBytes"]):
                raise ValueError("legacy archive index checksum or size mismatch")
            source.seek(0)
            first = source.readline()
            if first:
                manifest["firstReceivedAt"] = json.loads(first).get("receivedAt")
        return [manifest | {"logName": log.name, "_legacy": True}]


def _members(path: Path) -> list[dict[str, Any]]:
    """校验共享小时 tar 正文及元数据引用的外部索引，返回成员清单。"""
    metadata_path = _metadata_path(path)
    if not metadata_path.is_file():
        return _legacy_members(path)
    if metadata_path.stat().st_size > 1024 * 1024:
        raise ValueError("archive metadata is too large")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    members = metadata.get("members") if isinstance(metadata, dict) else None
    if not isinstance(metadata, dict) or metadata.get("formatVersion") != 2 or not isinstance(members, list) or not members:
        raise ValueError("invalid archive metadata")
    required = {"taskId", "runId", "sessionId", "hourStart", "rawSize", "sha256", "firstSequence", "lastSequence",
                "logName", "indexName", "indexSha256", "indexBytes"}
    expected = {}
    source_names: set[str] = set()
    for member in members:
        if not isinstance(member, dict) or not required <= member.keys():
            raise ValueError("archive metadata member is incomplete")
        source_name = member.get("sourceLogName", member["logName"])
        log_path = _member_path(path, source_name, ".log")
        member_path = _member_path(path, member["logName"], ".log")
        _member_path(path, member["indexName"], ".index.jsonl")
        if (member_path.name in expected or log_path.name in source_names
                or not isinstance(member["rawSize"], int)
                or not isinstance(member["indexBytes"], int)):
            raise ValueError("archive metadata member is invalid")
        expected[member_path.name] = member
        source_names.add(log_path.name)
    checked = set()
    # 一次顺序解压，不先遍历整个gzip再seek回第一成员重新解压。
    with tarfile.open(path, "r|gz") as archive:
        for info in archive:
            member = expected.get(info.name)
            if not info.isfile() or member is None or info.name in checked:
                raise ValueError("archive contains non-log or duplicate members")
            source = archive.extractfile(info)
            if source is None or _hash_stream(source) != (member["sha256"], member["rawSize"]):
                raise ValueError("archive log checksum or size mismatch")
            index_path = _member_path(path, member["indexName"], ".index.jsonl")
            with index_path.open("rb") as index:
                if _hash_stream(index) != (member["indexSha256"], member["indexBytes"]):
                    raise ValueError("archive index checksum or size mismatch")
                index.seek(0)
                first = index.readline()
                if first:
                    member.setdefault("firstReceivedAt", json.loads(first).get("receivedAt"))
            checked.add(info.name)
    if checked != set(expected):
        raise ValueError("archive members missing")
    return members


def _sidecar_member_names(path: Path) -> set[str] | None:
    """仅读取小型元数据以判断已登记归档是否可跳过逐字节恢复校验。"""
    metadata_path = _metadata_path(path)
    if not metadata_path.is_file() or metadata_path.stat().st_size > 1024 * 1024:
        return None
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    members = metadata.get("members") if isinstance(metadata, dict) else None
    if not isinstance(metadata, dict) or metadata.get("formatVersion") != 2 or not isinstance(members, list) or not members:
        return None
    names = {item.get("logName") for item in members if isinstance(item, dict)}
    return names if len(names) == len(members) and all(isinstance(name, str) for name in names) else None


def _ready_group_metadata(path: Path) -> tuple[set[str], int] | None:
    """在线程中配对读取旁路成员名和归档大小，避免维护协程触发磁盘 stat。"""
    names = _sidecar_member_names(path)
    return (names, path.stat().st_size) if names is not None else None


async def _ready_group_matches(repo: Any, path: Path) -> bool:
    """大小和成员数均未变化的完整 READY 组无需每次维护重新读取全部 tar 正文。"""
    metadata = await job_thread(_ready_group_metadata, path)
    if metadata is None:
        return False
    names, archive_bytes = metadata
    records = [record async for record in repo.db.files.find({"path": str(path), "status": "READY"})]
    return (len(records) == len(names) and {record.get("archiveMember") for record in records} == names
            and all(record.get("archiveBytes") == archive_bytes for record in records))


def _scanned_orphan_archives(root: Path) -> tuple[list[Path], list[Path], list[Path], dict[Path, float]]:
    """在线程中一次遍历日志树，返回排序后的归档恢复候选及 pending 文件时间。"""
    spools: list[Path] = []
    pending_journals: list[Path] = []
    archives: list[Path] = []
    modified: dict[Path, float] = {}
    for directory, names, files in os.walk(root):
        # 导出目录由下载作业生命周期管理，恢复扫描既不登记也不继续深入其中。
        names[:] = [name for name in names if name != "exports"]
        parent = Path(directory)
        for name in files:
            path = parent / name
            is_pending = False
            if name.endswith(".tar.gz.pending.json"):
                pending_journals.append(path)
                is_pending = True
            elif ".tar.gz.pending." in name and name.endswith(".json"):
                spools.append(path)
                is_pending = True
            elif name.endswith(".tar.gz"):
                archives.append(path)
            else:
                continue
            if is_pending:
                try:
                    modified[path] = path.stat().st_mtime
                except FileNotFoundError:
                    continue
    return sorted(spools), sorted(pending_journals), sorted(archives), modified


def _scanned_pending_archives(root: Path) -> tuple[list[Path], list[Path], dict[Path, float], list[Path]]:
    """仅在线程中枚举可恢复 spool 和旧 pending，不读取 tar、raw 或外部索引。"""
    spools: list[Path] = []
    pending_journals: list[Path] = []
    modified: dict[Path, float] = {}
    receipts: list[Path] = []
    for directory, names, files in os.walk(root):
        names[:] = [name for name in names if name != "exports"]
        parent = Path(directory)
        for name in files:
            path = parent / name
            if name.endswith(".tar.gz.catalog.pending.json"):
                receipts.append(path)
            elif name.endswith(".tar.gz.pending.json"):
                pending_journals.append(path)
            elif ".tar.gz.pending." in name and name.endswith(".json"):
                spools.append(path)
            else:
                continue
            try:
                modified[path] = path.stat().st_mtime
            except FileNotFoundError:
                continue
    return sorted(spools), sorted(pending_journals), modified, sorted(receipts)


def _members_and_archive_bytes(path: Path) -> tuple[list[dict[str, Any]], int]:
    """在同一工作线程完成重建校验后的归档大小读取。"""
    return _members(path), path.stat().st_size


async def pump_pending_archives(repo: Any) -> int:
    """每五秒独立补充持久归档槽位，避免历史 tar 校验或索引回填饿死 spool。"""
    spools, pending_journals, modified, receipts = await job_thread(_scanned_pending_archives, _root(repo))
    from camera_logs.node.performance_runtime import archive_spool_snapshot
    pending_targets: dict[Path, float] = {}
    scheduled = 0

    def record_pending(target: Path, source: Path) -> None:
        stamp = modified.get(source)
        if stamp is not None:
            key = target.resolve()
            pending_targets[key] = min(pending_targets.get(key, stamp), stamp)

    # 新 spool 与旧事务清单共用年龄顺序，避免升级遗留清单永远排在新格式之后。
    spool_paths = set(spools)
    candidates = sorted([*spools, *pending_journals],
                        key=lambda path: (modified.get(path, float("inf")), str(path)))
    for source in candidates:
        try:
            if source in spool_paths:
                target, _ = await asyncio.to_thread(load_archive_spool, source)
                record_pending(target, source)
                scheduled += int(schedule_pending_archive(source, recovery=True) is not None)
            else:
                target = Path(str(source).removesuffix(".pending.json"))
                record_pending(target, source)
                scheduled += int(schedule_recovery_archive(target) is not None)
        except Exception:
            record_pending(source, source)
            logger.exception("归档持久清单补充登记失败 path=%s", source)
    from camera_logs.logs.archive_catalog_recovery import reconcile_receipts
    compression_targets = set(pending_targets)
    for receipt in receipts:
        record_pending(Path(str(receipt).removesuffix(".catalog.pending.json")), receipt)
    completed = await reconcile_receipts(repo, receipts, _register_archive_members)
    for target in completed:
        if target not in compression_targets:
            pending_targets.pop(target, None)
    current = now().timestamp()
    oldest_seconds = max(0, current - min(pending_targets.values())) if pending_targets else 0
    archive_spool_snapshot(len(pending_targets), oldest_seconds)
    return scheduled


async def _register_archive_members(repo: Any, path: Path, members: list[dict], archive_bytes: int) -> int:
    """复用已完成校验的成员更新目录，保留身份CAS及删除状态，刷新共享包大小。"""
    root = _root(repo)
    _contained(repo, path)
    registered = 0
    group_id = hashlib.sha256(str(path.relative_to(root)).encode()).hexdigest()
    identifiers = [_archive_id(root, _member_path(path, member.get("sourceLogName", member["logName"]), ".log"), node)
                   for member in members for node in (None, repo.settings.node_id)]
    existing_records = {record["id"]: record async for record in repo.db.files.find({"id": {"$in": identifiers}})}
    for member in members:
        log_path = _member_path(path, member.get("sourceLogName", member["logName"]), ".log")
        index_path = None if member.get("_legacy") else _member_path(path, member["indexName"], ".index.jsonl")
        identifier = _archive_id(root, log_path, repo.settings.node_id)
        existing = existing_records.get(identifier)
        legacy_id = _archive_id(root, log_path)
        legacy = existing_records.get(legacy_id)
        # 旧URL保持稳定；外节点同名路径绝不能被接管或覆盖。缺身份的旧删除标记保守保留。
        if existing is None and legacy and legacy.get("nodeId") in (None, repo.settings.node_id):
            identifier, existing = legacy_id, legacy
        if existing and existing.get("status") not in {"OPEN", "READY"}:
            continue
        identity = {key: member[key] for key in ("taskId", "runId", "sessionId")}
        identity["nodeId"] = repo.settings.node_id
        if existing and any(existing.get(key) != value for key, value in identity.items()):
            raise ValueError("archive identity differs from open catalog record")
        document = {
            "id": identifier, **identity,
            "hour": datetime.fromisoformat(member["hourStart"]).astimezone(UTC).isoformat(),
            "path": str(path), "archiveName": path.name, "archiveMember": member["logName"],
            "archiveGroupId": group_id, "rawFileName": member["logName"],
            "bytes": member["rawSize"], "archiveBytes": archive_bytes, "sha256": member["sha256"],
            "firstSequence": member["firstSequence"], "lastSequence": member["lastSequence"],
            "segmentNumber": int(match.group(1)) if not member.get("_legacy") and (
                match := re.search(r"-part-(\d+)\.log$", log_path.name)) else 0,
            "status": "READY", "updatedAt": now(),
        }
        if index_path is not None:
            document["indexPath"] = str(index_path)
        for key in ("firstReceivedAt", "lastReceivedAt"):
            if member.get(key):
                document[key] = member[key]
        if existing and all(existing.get(key) == value for key, value in document.items() if key != "updatedAt"):
            # 超时后的下一轮跳过已完成成员，不能每次从头更新导致大包永远无法确认。
            continue
        if existing:
            result = await repo.db.files.update_one(
                {"id": identifier, "status": {"$in": ["OPEN", "READY"]}, **identity}, {"$set": document})
            registered += result.modified_count
        else:
            result = await repo.db.files.update_one(
                {"id": identifier, **identity, "status": {"$in": ["OPEN", "READY"]}},
                {"$set": document, "$setOnInsert": {"createdAt": now()}}, upsert=True)
            registered += int(result.upserted_id is not None)
    return registered


async def recover_orphan_archives(repo: Any) -> int:
    """节点重启后续交 pending 归档并补回已发布归档的目录记录。"""
    root = _root(repo)
    registered = 0
    # 新封存组各有不可变 spool；恢复阶段只放入与实时相同的有界调度器，不能让
    # 节点启动等待积压小时包。发布后的 metadata 仍在后续维护周期回写目录记录。
    spooled_logs: set[Path] = set()
    spools, pending_journals, archives, pending_modified = await job_thread(_scanned_orphan_archives, root)
    pending_targets: dict[Path, float] = {}

    def record_pending(target: Path, source: Path) -> None:
        """以归档目标为恢复队列单位，同目标的 spool 与 journal 不重复计数。"""
        modified = pending_modified.get(source)
        if modified is None:
            return
        key = target.resolve()
        pending_targets[key] = min(pending_targets.get(key, modified), modified)

    for spool in spools:
        try:
            target, segments = await asyncio.to_thread(load_archive_spool, spool)
            record_pending(target, spool)
            spooled_logs.update(Path(segment["logPath"]).resolve() for segment in segments
                                if isinstance(segment.get("logPath"), str))
            # spool 是已封存的不可变分卷；即便同一会话仍在采集，也可以交给
            # 按目标串行的调度器发布，不能因活动任务让队满漏项永久遗留。
            schedule_pending_archive(spool)
        except Exception:
            # 格式损坏的 spool 不能丢弃，也以自身路径作为一个待人工/后续恢复的作业。
            record_pending(spool, spool)
            logger.exception("归档 spool 恢复登记失败 path=%s", spool)
    for pending in pending_journals:
        record_pending(Path(str(pending).removesuffix(".pending.json")), pending)
    await _recover_proven_raw_segments(repo, spooled_logs)
    # 提交清单先于 tar 原子替换落盘。仅扫描已有 tar 会遗漏此后断电时留下的
    # ``.tar.gz.pending.json``，因此将两类路径归一为同一个归档目标并去重处理。
    paths = set(archives)
    for pending in pending_journals:
        paths.add(Path(str(pending).removesuffix(".pending.json")))
    for path in sorted(paths):
        if "exports" in path.parts:
            continue
        try:
            pending = await asyncio.to_thread(_pending_segments, path)
            if pending is not None:
                schedule_recovery_archive(path)
                # 旧提交清单可能还没有 tar；启动阶段只登记恢复作业，下一维护
                # 周期再从 metadata 回写目录，不能因历史积压同步等待。
                continue
            if await _ready_group_matches(repo, path):
                continue
            from camera_logs.node.performance_runtime import (
                archive_finished,
                archive_running,
                archive_started,
            )
            token = object()
            archive_started(token)
            archive_running(token)
            try:
                members, archive_bytes = await job_thread(_members_and_archive_bytes, path)
                registered += await _register_archive_members(repo, path, members, archive_bytes)
            finally:
                archive_finished(token)
        except Exception:
            logger.exception("归档恢复失败 path=%s", path)
    return registered


async def apply_retention(repo: Any, *, development_file_ids: frozenset[str] | None = None) -> dict[str, int]:
    """领取整组归档后删除；开发入口仅传入已通过报告/内容校验的有限 ID 集合。

    开发清理只绕过自然保留天数，仍受下载保护、作业、节点和成员组约束，且任务
    必须已随资源删除而永久停用。正常维护不传集合，继续使用平台保留天数。
    """
    cutoff = (now() if development_file_ids is not None else
              now() - timedelta(days=await get_retention_days(repo)) - timedelta(hours=1))
    removed = skipped = failures = 0
    query = {
        "nodeId": repo.settings.node_id, "$or": [
            {"status": "READY", "hour": {"$lt": utc_iso(cutoff)}},
            {"status": "DELETING"},
        ],
    }
    if development_file_ids is not None:
        query["id"] = {"$in": sorted(development_file_ids)}
    cursor = repo.db.files.find(query)
    async for document in cursor:
        # 一个共享小时 tar 可被多个 files 成员引用。先读取完整成员组，任一
        # 受保护、未过期、非本节点或未进入可删除状态都阻止物理删除。
        if (_pending_path(Path(document["path"])).exists()
                or Path(str(document["path"]) + ".catalog.pending.json").exists()):
            skipped += 1
            continue
        group = [item async for item in repo.db.files.find({"path": document["path"]})]
        eligible = bool(group)
        for member in group:
            if development_file_ids is not None:
                task = await repo.db.tasks.find_one({"id": member.get("taskId"),
                    "resourceDeleted": True, "status": "STOPPED", "desiredState": "STOPPED", "nodeId": None})
                if member["id"] not in development_file_ids or task is None:
                    eligible = False
                    break
            if member.get("nodeId") != repo.settings.node_id or member.get("status") not in {"READY", "DELETING"}:
                eligible = False
                break
            if member.get("status") == "READY":
                if member.get("hour", utc_iso(cutoff)) >= utc_iso(cutoff):
                    eligible = False
                    break
                retain_until = member.get("retainUntil")
                if retain_until is not None:
                    if not isinstance(retain_until, datetime):
                        eligible = False
                        break
                    retain_until = retain_until.replace(tzinfo=UTC) if retain_until.tzinfo is None else retain_until
                    if retain_until > now():
                        eligible = False
                        break
                active = await repo.db.jobs.find_one({
                    "status": {"$in": ["QUEUED", "RUNNING"]}, "files.id": member["id"]
                })
                if active:
                    eligible = False
                    break
        if not eligible:
            skipped += 1
            continue
        for member in group:
            if member.get("status") != "READY":
                continue
            claimed = await repo.db.files.find_one_and_update(
                {"id": member["id"], "status": "READY", "$or": [
                    {"retainUntil": None}, {"retainUntil": {"$lte": now()}},
                ]},
                {"$set": {"status": "DELETING", "retentionClaimedAt": now()}},
                return_document=ReturnDocument.AFTER,
            )
            if not claimed:
                eligible = False
                break
        if not eligible:
            skipped += 1
            continue
        claimed_group = [item async for item in repo.db.files.find({"path": document["path"]})]
        if not claimed_group or any(item.get("status") != "DELETING" for item in claimed_group):
            skipped += 1
            continue
        try:
            path = _contained(repo, document["path"])
            if path.suffixes[-2:] != [".tar", ".gz"]:
                raise ValueError("retention only deletes published archives")
            cleanup_path = Path(document["path"])
            cleanup_path = cleanup_path if cleanup_path.is_absolute() else Path(repo.settings.log_root) / cleanup_path
            cleanup_allowed = path.exists() and not path.is_symlink()
            await asyncio.to_thread(path.unlink, missing_ok=True)
            sidecars = {_metadata_path(path), index_path(path), Path(str(path) + ".catalog.pending.json")}
            for member in claimed_group:
                if member.get("indexPath"):
                    sidecars.add(_contained(repo, member["indexPath"]))
            await asyncio.gather(*(asyncio.to_thread(sidecar.unlink, missing_ok=True) for sidecar in sidecars))
            deleted = await repo.db.files.delete_many({"path": str(path), "status": "DELETING"})
            removed += deleted.deleted_count
            if cleanup_allowed:
                await asyncio.to_thread(_remove_empty_log_ancestors, Path(repo.settings.log_root), cleanup_path)
        except Exception:
            failures += 1
            logger.exception("归档保留清理失败 path=%s", document.get("path"))
            await repo.db.files.update_many(
                {"path": document["path"], "status": "DELETING"},
                {"$set": {"retentionErrorAt": now()}},
            )
    return {"removed": removed, "skipped": skipped, "failures": failures}


async def get_retention_days(repo: Any) -> int:
    """每轮维护读取数据库保留期；缺省或损坏记录安全回退到进程默认值。"""
    fallback = int(getattr(repo.settings, "retention_days", 7))
    collection = getattr(repo.db, "platform_settings", None)
    if collection is None:
        return fallback
    document = await collection.find_one({"id": "platform"})
    value = document.get("retentionDays") if document else fallback
    if isinstance(value, int) and 1 <= value <= 3650:
        return value
    logger.warning("平台保留期配置无效，使用默认值 retentionDays=%s", fallback)
    return fallback


async def _remove_export_tree(path: Path) -> None:
    """取消维护协程时仍等待删除线程退出，过期 token 接管不会与旧线程并行。"""
    deletion = asyncio.create_task(asyncio.to_thread(shutil.rmtree, path))
    cancelled = False
    while not deletion.done():
        try:
            await asyncio.shield(deletion)
        except asyncio.CancelledError:
            # 每次取消都先 drain 同一个 rmtree 线程；不能启动第二个删除线程。
            cancelled = True
    await deletion
    if cancelled:
        raise asyncio.CancelledError


async def _recover_expired_export_cleanup_claims(repo: Any, stamp: datetime) -> None:
    """每轮最多恢复100个过期清理权，避免故障遗留记录占满一次维护。"""
    stale = {
        "nodeId": repo.settings.node_id,
        "kind": "DOWNLOAD",
        "status": {"$in": ["SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"]},
        "outputLockProtocol": 1,
        "outputCleanupState": "CLEANING",
        "$or": [
            {"outputCleanupLeaseUntil": {"$lte": stamp}},
            {"outputCleanupLeaseUntil": {"$exists": False}},
        ],
    }
    rows = [job async for job in repo.db.jobs.find(stale, {"id": 1}).sort(
        [("outputCleanupLeaseUntil", 1), ("id", 1)]
    ).limit(EXPORT_CLEANUP_BATCH_SIZE)]
    for job in rows:
        await repo.db.jobs.update_one(
            {"id": job["id"], **stale},
            {"$set": {"outputCleanupState": "IDLE"},
             "$unset": {"outputCleanupToken": "", "outputCleanupLeaseUntil": ""}},
        )


async def cleanup_exports(repo: Any) -> int:
    """受写入关闭证据和读者租约保护地回收已到期下载目录。

    清理器先在作业行上 CAS 到 CLEANING；读者领取租约使用同一栅栏，故不能
    在文件描述符已经打开后删除目录。WRITING 仅在取得同一内核排他锁后恢复，
    旧作业、缺少归属元数据及未知状态保留，避免误删仍被旧进程写入的路径。
    """
    configured_root = Path(repo.settings.log_root)
    exports, scratch_root = configured_root / "exports", configured_root / "exports" / ".tmp"
    if configured_root.is_symlink() or exports.is_symlink() or scratch_root.is_symlink():
        logger.warning("拒绝清理包含软链接的导出根 path=%s", exports)
        return 0
    removed, stamp = 0, now()
    await _recover_expired_export_cleanup_claims(repo, stamp)
    active = {
        "nodeId": repo.settings.node_id,
        "kind": "DOWNLOAD",
        "status": {"$in": ["SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"]},
        "expiresAt": {"$lte": stamp},
        "outputExecutionState": {"$in": ["CLOSED", "WRITING"]},
        "outputWriterNodeId": repo.settings.node_id,
        "outputLockProtocol": 1,
    }
    node = await repo.db.nodes.find_one({"id": repo.settings.node_id}, {"exportCleanupCursor": 1}) or {}
    bookmark = node.get("exportCleanupCursor")
    if not isinstance(bookmark, dict) or not isinstance(bookmark.get("expiresAt"), datetime) or not isinstance(bookmark.get("id"), str):
        bookmark = None
    query: dict[str, Any] = {**active, "outputCleanupState": {"$in": ["IDLE", None]}}
    if bookmark is not None:
        query = {"$and": [query, {"$or": [
            {"expiresAt": {"$gt": bookmark["expiresAt"]}},
            {"expiresAt": bookmark["expiresAt"], "id": {"$gt": bookmark["id"]}},
        ]}]}
    page = [job async for job in repo.db.jobs.find(query).sort(
        [("expiresAt", 1), ("id", 1)]
    ).limit(EXPORT_CLEANUP_BATCH_SIZE)]
    if not page:
        await repo.db.nodes.update_one(
            {"id": repo.settings.node_id},
            {"$set": {"exportCleanupCursor": None}},
            upsert=True,
        )
        return removed
    for job in page:
        candidate = {"id": job["id"], **active, "outputCleanupState": {"$in": ["IDLE", None]}}
        for path in (exports / job["id"], scratch_root / job["id"]):
            if not path.exists():
                continue
            cleanup_token: str | None = None
            completed = False
            try:
                expected_parent = scratch_root.resolve() if path.parent == scratch_root else exports.resolve()
                if path.is_symlink() or not path.is_dir() or path.resolve().parent != expected_parent:
                    continue
                await repo.db.jobs.update_one(candidate, {"$pull": {"outputReaders": {"expiresAt": {"$lte": stamp}}}})
                cleanup_token = uuid.uuid4().hex
                claimed = await repo.db.jobs.update_one({**candidate,
                    "outputReaders": {"$not": {"$elemMatch": {"expiresAt": {"$gt": stamp}}}}},
                    {"$set": {"outputCleanupState": "CLEANING", "outputCleanupToken": cleanup_token,
                              "outputCleanupLeaseUntil": stamp + timedelta(minutes=30)}},
                )
                if claimed.matched_count != 1:
                    continue
                lock = ExportLock(repo, job["id"])
                if not await lock.acquire(blocking=False):
                    continue
                try:
                    if job.get("outputExecutionState") == "WRITING":
                        closed = await repo.db.jobs.update_one(
                            {"id": job["id"], **active, "outputExecutionState": "WRITING",
                             "outputCleanupState": "CLEANING", "outputCleanupToken": cleanup_token},
                            {"$set": {"outputExecutionState": "CLOSED", "outputWriterClosedAt": now(),
                                      "outputWriterClosureReason": "KERNEL_LOCK_RECOVERY"}},
                        )
                        if closed.matched_count != 1:
                            continue
                    await _remove_export_tree(path)
                    other = scratch_root / job["id"] if path.parent == exports else exports / job["id"]
                    if other.exists() and not other.is_symlink() and other.is_dir():
                        await _remove_export_tree(other)
                finally:
                    await lock.close()
                await repo.db.jobs.update_one(
                    {"id": job["id"], **active, "outputExecutionState": "CLOSED",
                     "outputCleanupState": "CLEANING", "outputCleanupToken": cleanup_token},
                    {"$set": {"outputCleanupState": "CLEANED", "outputCleanedAt": now()}})
                completed = True
                removed += 1
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("导出产物清理失败 path=%s", path)
            finally:
                if cleanup_token and not completed:
                    await repo.db.jobs.update_one(
                        {"id": job["id"], **active, "outputCleanupState": "CLEANING",
                         "outputCleanupToken": cleanup_token},
                        {"$set": {"outputCleanupState": "IDLE"}, "$unset": {"outputCleanupToken": "", "outputCleanupLeaseUntil": ""}})
    last = page[-1]
    await repo.db.nodes.update_one(
        {"id": repo.settings.node_id},
        {"$set": {"exportCleanupCursor": {"expiresAt": last["expiresAt"], "id": last["id"]}}},
        upsert=True,
    )
    return removed


async def maintain(repo: Any) -> dict[str, int]:
    recovered = await recover_orphan_archives(repo)
    retention = await apply_retention(repo)
    exports = await cleanup_exports(repo)
    return {"recovered": recovered, "exportsRemoved": exports, **retention}
