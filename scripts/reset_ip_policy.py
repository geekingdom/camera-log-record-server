"""在本机或显式配置的 MongoDB 上恢复平台客户端 IP 白名单为关闭状态。

默认仅输出计划；发生来源地址自锁时，执行
`python scripts/reset_ip_policy.py --apply`，才会写入关闭状态和恢复审计记录。
容器副本集必须同时提供 `--allow-configured-database`，且只能使用当前 Settings 的 MONGO_URI。
"""

import argparse
import asyncio
import ipaddress

from camera_logs.access_policy.policy import POLICY_ID
from camera_logs.common.config import Settings
from camera_logs.common.database import now
from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.uri_parser import parse_uri


def _local_mongo(uri: str) -> bool:
    """只接受全部节点均为 loopback 的普通 Mongo URI，拒绝远程和 SRV 目标。"""
    if uri.startswith("mongodb+srv://"):
        return False
    try:
        nodes = parse_uri(uri)["nodelist"]
    except Exception:  # noqa: BLE001 - 解析失败也必须阻止恢复写入。
        return False
    if not nodes:
        return False
    for host, _port in nodes:
        if host.lower() == "localhost":
            continue
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return False
        except ValueError:
            return False
    return True


def database_allowed(settings: Settings, allow_configured_database: bool) -> bool:
    """默认只允许本机节点；显式开关仅放行 Settings 中实际配置的非本机 URI。"""
    if _local_mongo(settings.mongo_uri):
        return True
    return allow_configured_database and "mongo_uri" in settings.model_fields_set


async def reset_ip_policy(database):
    """关闭单例策略并递增版本；直接写审计保证锁死恢复仍可追溯。"""
    timestamp = now()
    policy = await database.ip_policy.find_one_and_update(
        {"id": POLICY_ID},
        {"$set": {"enabled": False, "updatedAt": timestamp},
         "$setOnInsert": {"id": POLICY_ID, "rules": [], "createdAt": timestamp},
         "$inc": {"version": 1}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    await database.audit.insert_one({
        "actor": "local-ip-policy-recovery", "action": "reset_ip_policy", "targetId": POLICY_ID,
        "createdAt": timestamp,
    })
    return policy


async def main() -> None:
    """解析显式确认参数，并仅连接本机配置的 MongoDB 后执行恢复。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="关闭平台 IP 白名单并写入恢复审计")
    parser.add_argument("--allow-configured-database", action="store_true",
                        help="仅允许使用当前 Settings 中显式配置的非本机 MONGO_URI")
    args = parser.parse_args()
    settings = Settings()
    if not database_allowed(settings, args.allow_configured_database):
        raise RuntimeError("拒绝操作：非本机 MONGO_URI 需要 --allow-configured-database，且必须来自当前 Settings 配置")
    if not args.apply:
        print(f"演练：将关闭 database={settings.database_name} 的平台 IP 白名单；使用 --apply 执行")
        return
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        policy = await reset_ip_policy(client[settings.database_name])
    finally:
        await client.close()
    print(f"已关闭平台 IP 白名单 database={settings.database_name} version={policy['version']}")


if __name__ == "__main__":
    asyncio.run(main())
