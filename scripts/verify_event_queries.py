"""在随机 MongoDB 库验证事件派生筛选、稳定分页与分页后的名称关联。"""

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from camera_logs.administration.event_queries import event_page, runtime_event_page
from camera_logs.common.config import Settings
from pymongo import AsyncMongoClient


def check(condition: bool, message: str) -> None:
    """以中文断言说明验证失败原因，便于 CI 和本机直接定位。"""
    if not condition:
        raise AssertionError(message)


async def main() -> None:
    """写入可销毁事件样本，验证 Mongo 实际聚合结果并在 finally 删除随机数据库。"""
    configured = Settings()
    database_name = f"event_queries_verify_{uuid4().hex}"
    client = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                              w="majority", journal=True)
    db = client[database_name]
    stamp = datetime(2026, 9, 9, tzinfo=UTC)
    try:
        await db.tasks.insert_many([{"id": f"task-{index}", "name": f"验证任务 {index}", "ip": "192.0.2.1"}
                                    for index in range(1000)])
        await db.events.insert_many([
            {"id": "gap", "type": "CONNECTION_GAP", "createdAt": stamp},
            {"id": "debug-empty", "type": "DEBUG_MODE", "debugError": "", "createdAt": stamp},
            {"id": "debug-failed", "type": "DEBUG_MODE", "debugError": "连接失败", "createdAt": stamp},
            {"id": "explicit", "type": "CONNECTION_GAP", "outcome": "FAILED", "level": "WARNING", "createdAt": stamp},
        ] + [{"id": f"bulk-{index}", "taskId": f"task-{index}", "type": "CONNECTION_GAP", "createdAt": stamp}
             for index in range(1000)])
        warning = await runtime_event_page(db, {"level": "WARNING", "outcome": "UNKNOWN"}, 1, 100)
        failed = await runtime_event_page(db, {"level": "ERROR", "outcome": "FAILED"}, 1, 100)
        explicit = await runtime_event_page(db, {"level": "WARNING", "outcome": "FAILED"}, 1, 100)
        second_page = await runtime_event_page(db, {"outcome": "UNKNOWN"}, 2, 20)
        debug_items = await runtime_event_page(db, {"type": "DEBUG_MODE"}, 1, 100)
        check(warning["total"] == 1001, "UNKNOWN/WARNING 聚合筛选不一致")
        check(failed["total"] == 1 and failed["items"][0]["id"] == "debug-failed", "失败调试事件筛选不一致")
        check(explicit["total"] == 1 and explicit["items"][0]["id"] == "explicit", "显式结果或级别优先级不一致")
        check(second_page["total"] == 1001 and len(second_page["items"]) == 20, "派生筛选分页或计数不正确")
        empty = next(item for item in debug_items["items"] if item["id"] == "debug-empty")
        check(empty["outcome"] == "SUCCEEDED", "空调试错误被误判为失败")
        await db.audit.insert_many([
            {"action": "coredump_export_failed", "targetId": "core-failed", "createdAt": stamp},
            {"action": "coredump_export_cancelled", "targetId": "core-cancelled", "createdAt": stamp},
        ])
        failures = await event_page(db, "audit", {"outcome": "FAILED", "level": "ERROR"}, 1, 10)
        cancellations = await event_page(db, "audit", {"outcome": "CANCELLED"}, 1, 10)
        check(failures["total"] == 1 and failures["items"][0]["summary"] == "核心转储导出失败",
              "核心转储失败的Mongo筛选与展示不一致")
        check(cancellations["total"] == 1 and cancellations["items"][0]["summary"] == "核心转储导出已取消",
              "核心转储取消的Mongo筛选与展示不一致")
        await db.audit.insert_many([
            {"action": action, "targetId": "device-audit", "createdAt": stamp}
            for action in (
                "authenticate_resource_credentials_rejected", "authenticate_resource_device_error",
                "slave-ssh-bootstrap-host-attempt", "slave-ssh-bootstrap-temporary-attempt",
                "slave-ssh-service-ready", "slave-ssh-connected",
            )
        ])
        device_query = {"targetId": "device-audit"}
        failures = await event_page(db, "audit", device_query | {"outcome": "FAILED", "level": "ERROR"}, 1, 10)
        unknown = await event_page(db, "audit", device_query | {"outcome": "UNKNOWN", "level": "WARNING"}, 1, 10)
        success = await event_page(db, "audit", device_query | {"outcome": "SUCCEEDED"}, 1, 10)
        check(failures["total"] == 2 and all(item["summary"].startswith("设备身份认证失败") for item in failures["items"]),
              "认证失败的Mongo筛选与中文展示不一致")
        check(unknown["total"] == 2 and all(item["summary"].endswith("等待确认") for item in unknown["items"]),
              "主从引导尝试不能按成功事件返回")
        check(success["total"] == 2 and {item["action"] for item in success["items"]} == {
            "slave-ssh-service-ready", "slave-ssh-connected"}, "主从成功筛选混入了尚未确认或失败动作")
        print("事件聚合验证通过：随机数据库已完成派生筛选、分页和优先级检查")
    finally:
        await client.drop_database(database_name)
        check(database_name not in await client.list_database_names(), "随机验证数据库未被清理")
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
