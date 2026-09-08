"""在服务和节点完全停止后，受控清理开发数据库中的任务历史和日志目录。"""

import argparse
import asyncio
import shutil
from pathlib import Path

from camera_logs.common.config import Settings
from pymongo import AsyncMongoClient

RESET_COLLECTIONS = (
    "tasks", "commands", "files", "runs", "jobs", "operations", "idempotency",
    "audit", "runtime_events", "download_sessions", "resources",
)
DROP_COLLECTIONS = ("endpoint_locks",)
PRESERVED_COLLECTIONS = ("tokens", "nodes", "node_configs", "platform_settings", "templates")


async def _assert_stopped(database) -> None:
    """仅当任务已停止且无节点归属、无未结束运行时才允许删除开发数据。"""
    active_task = await database.tasks.find_one({"$or": [
        {"nodeId": {"$exists": True, "$ne": None}},
        {"desiredState": {"$ne": "STOPPED"}},
        {"status": "RUNNING"},
    ]})
    if active_task:
        raise RuntimeError("存在活动任务；请先通过 API 停止所有任务并等待节点归属释放")
    active_run = await database.runs.find_one({"endedAt": {"$exists": False}})
    if active_run:
        raise RuntimeError("存在活动运行；请等待运行结束后再重置开发数据")


def _validated_log_root(log_root: Path) -> Path:
    """解析并验证日志根目录，拒绝文件系统根目录和非目录目标。"""
    root = log_root.resolve(strict=False)
    if root == Path(root.anchor):
        raise RuntimeError("日志根路径不能是文件系统根目录")
    if root.exists() and not root.is_dir():
        raise RuntimeError(f"日志根路径不是目录：{root}")
    return root


def _clear_log_root(log_root: Path) -> int:
    """仅删除已验证日志根目录的直接子项，不触及服务日志、密钥或根目录本身。"""
    root = _validated_log_root(log_root)
    if not root.exists():
        return 0
    removed = 0
    for child in root.iterdir():
        if child.is_symlink() or child.is_file():
            child.unlink()
        else:
            shutil.rmtree(child)
        removed += 1
    return removed


async def reset_development_data(database, log_root: Path) -> dict[str, int | str]:
    """校验停机条件后清理任务历史和资源，保留访问、节点、平台及模板配置。"""
    await _assert_stopped(database)
    _validated_log_root(log_root)
    deleted = {}
    for name in RESET_COLLECTIONS:
        result = await database[name].delete_many({})
        deleted[name] = result.deleted_count
    for name in DROP_COLLECTIONS:
        await database.drop_collection(name)
        deleted[name] = 0
    return {"database": database.name, "logEntries": _clear_log_root(log_root), **deleted}


async def main() -> None:
    """从当前 Settings 的精确数据库和日志根读取目标，必须显式确认才执行删除。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true", help="确认服务和所有采集节点已停止，执行不可恢复的开发重置")
    args = parser.parse_args()
    settings = Settings()
    if not args.confirm:
        print("未执行：请确认 API 与采集节点已停止后，使用 --confirm 开发重置")
        return
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        result = await reset_development_data(client[settings.database_name], settings.log_root)
    finally:
        await client.close()
    print(f"开发数据已重置 database={result['database']} logs={result['logEntries']}")


if __name__ == "__main__":
    asyncio.run(main())
