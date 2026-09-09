"""按显式压测报告核验并清理本节点合成归档，默认只预览且保留下载保护期。"""

import argparse
import asyncio
import fcntl
import json
from datetime import UTC, datetime
from pathlib import Path

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.logs.maintenance import apply_retention
from dev_cleanup_evidence import load_evidence
from pymongo import AsyncMongoClient
from service_benchmark_io import verify_download


def checked_path(root: Path, value: str) -> Path:
    """只接受根目录内的普通文件，拒绝所有层级符号链接与相对路径歧义。"""
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("开发清理只接受目录记录中的绝对路径")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("清理路径包含符号链接")
    path = path.resolve(strict=True)
    if root not in path.parents or not path.is_file():
        raise ValueError("文件不在日志根目录内或不是普通文件")
    return path


async def plan_cleanup(repo, evidence: dict) -> dict:
    """核对来源、永久停用状态、完整归档组及实际正文，返回有限文件 ID 清单。"""
    root = Path(repo.settings.log_root).resolve(strict=True)
    resource = await repo.db.resources.find_one({"id": evidence["resourceId"]})
    if not resource or not resource.get("deletedAt") or resource.get("deletionState") != "DONE":
        raise ValueError("报告资源尚未完成软删除")
    task_ids = set(evidence["tasks"])
    files = []
    for identifier in sorted(task_ids):
        task = await repo.db.tasks.find_one({"id": identifier})
        if (not task or task.get("resourceId") != resource["id"] or not task.get("resourceDeleted")
            or task.get("status") != "STOPPED" or task.get("desiredState") != "STOPPED"
            or task.get("nodeId") is not None or task.get("port") != evidence["taskPorts"][identifier]):
            raise ValueError("报告任务绑定或停止状态不符合开发清理条件")
        members = [item async for item in repo.db.files.find({"taskId": identifier})]
        if not members:
            raise ValueError("报告任务已无完整目录记录，不能重复推断已删除文件")
        files.extend(members)
    ids = {item["id"] for item in files}
    paths = {}
    for item in files:
        if item.get("nodeId") != repo.settings.node_id or item.get("status") != "READY":
            raise ValueError("文件未就绪或不属于当前采集节点")
        path = checked_path(root, item["path"])
        if path.suffixes[-2:] != [".tar", ".gz"] or Path(str(path) + ".pending.json").exists():
            raise ValueError("文件不是已完成发布的小时归档")
        paths[str(path)] = path
        index = checked_path(root, item["indexPath"])
        if index.parent != path.parent or not index.name.endswith(".index.jsonl"):
            raise ValueError("索引路径不属于同一小时目录")
        if await repo.db.files.find_one({"indexPath": str(index), "id": {"$nin": sorted(ids)}}):
            raise ValueError("索引被未批准的目录记录引用")
    for value, path in paths.items():
        group = [item async for item in repo.db.files.find({"path": value})]
        if not group or any(item["id"] not in ids for item in group):
            raise ValueError("共享归档包含报告范围外的成员")
        metadata_path = checked_path(root, str(path) + ".metadata.json")
        if metadata_path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("归档成员清单过大")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        members = metadata.get("members", [])
        expected = {(item["taskId"], item["runId"], item["sessionId"], item["archiveMember"]) for item in group}
        actual = {(item["taskId"], item["runId"], item["sessionId"], item["logName"]) for item in members}
        if metadata.get("formatVersion") != 2 or actual != expected or len(members) != len(group):
            raise ValueError("归档清单与完整目录成员不一致")
    # 使用采集验收器校验实际解压正文，而不是仅相信历史报告的成功标记。
    for identifier, result in evidence["tasks"].items():
        ordered = sorted((item for item in files if item["taskId"] == identifier),
                         key=lambda item: (item["hour"], item.get("firstReceivedAt", ""), item["path"]))
        task_paths = list(dict.fromkeys(paths[item["path"]] for item in ordered))
        await asyncio.to_thread(verify_download, task_paths, result["sourceSha256"], result["sourceLines"])
    protected = []
    for item in files:
        until = item.get("retainUntil")
        if until is not None:
            if not isinstance(until, datetime):
                raise ValueError("下载保护时间类型无效")
            until = until.replace(tzinfo=UTC) if until.tzinfo is None else until
            if until > now():
                protected.append({"fileId": item["id"], "until": until.isoformat()})
    active = await repo.db.jobs.count_documents({"status": {"$in": ["QUEUED", "RUNNING"]},
                                                "files.id": {"$in": sorted(ids)}})
    return {"reportSha256": evidence["reportSha256"], "resourceId": resource["id"],
            "fileIds": sorted(ids), "archives": len(paths), "bytes": sum(path.stat().st_size for path in paths.values()),
            "protected": protected, "activeJobs": active}


async def cleanup_report(repo, directory: Path, *, apply: bool = False) -> dict:
    """执行前重跑完整预检；记录开发操作审计，下载领取由维护函数的 CAS 再次防护。"""
    plan = await plan_cleanup(repo, load_evidence(directory))
    outcome = {"mode": "apply" if apply else "dry-run", **plan}
    if not apply or plan["protected"] or plan["activeJobs"]:
        outcome["result"] = "PROTECTED" if plan["protected"] or plan["activeJobs"] else "ELIGIBLE"
        return outcome
    await repo.audit("development-cleanup", "cleanup_synthetic_logs_requested", plan["resourceId"])
    result = await apply_retention(repo, development_file_ids=frozenset(plan["fileIds"]))
    await repo.audit("development-cleanup", "cleanup_synthetic_logs_finished", plan["resourceId"])
    return {**outcome, "result": "FINISHED", "cleanup": result}


async def run(args):
    """使用本节点配置连接数据库，不初始化/迁移数据；所有输入报告必须显式给定。"""
    settings = Settings()
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        repo = Repository(client[settings.database_name], settings)
        results = []
        for directory in args.report:
            results.append(await cleanup_report(repo, directory, apply=args.apply))
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        await client.close()


def main():
    """锁住同机开发清理进程；默认预览不会更新文件目录或业务审计。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, action="append", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    root = Path(Settings().log_root).resolve(strict=True)
    with (root / ".development-cleanup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
