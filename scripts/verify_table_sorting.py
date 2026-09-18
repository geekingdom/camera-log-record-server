"""随机专用 Mongo 库验证资源/任务排序和 explain，结束后只删除本脚本创建的库。"""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.resources.listing import page_resources, resource_list_pipeline
from camera_logs.tasks.api import _page_tasks
from pymongo import AsyncMongoClient


async def main() -> None:
    settings = Settings()
    database = f"verify_table_sorting_{uuid4().hex}"
    client = AsyncMongoClient(settings.mongo_uri, tz_aware=True)
    db = client[database]
    try:
        resources = [{"id": f"resource-{number:02}", "name": "同名", "model": "M", "subSerialNumber": str(number),
                      "ip": f"192.0.2.{number}", "deletedAt": None, "createdAt": f"2026-09-10T{number % 10:02}:00:00Z"}
                     for number in range(32)]
        for number, resource in enumerate(resources):
            resource["createdByName" if number % 2 else "createdBy"] = f"creator-{31 - number:02}"
        await db.resources.insert_many(resources)
        tasks = []
        for number in range(32):
            for sequence in range(number % 4):
                tasks.append({"id": f"task-{number:02}-{sequence}", "name": f"task-{number:02}", "ip": f"192.0.2.{number}",
                              "port": 22, "protocol": "SSH", "resourceId": f"resource-{number:02}", "status": "STOPPED",
                              "desiredState": "STOPPED", "createdAt": f"2026-09-10T{number % 10:02}:00:00Z"})
        # 同任务双绑定同一资源不能使资源任务数翻倍。
        tasks.append({"id": "double-bound", "name": "task-double", "ip": "192.0.2.31", "port": 23, "protocol": "TELNET_SERIAL",
                      "resourceId": "resource-31", "serialServerResourceId": "resource-31", "status": "STOPPED", "desiredState": "STOPPED"})
        await db.tasks.insert_many(tasks)
        await db.coredump_files.insert_many([{"id": f"core-{number}", "resourceId": f"resource-{number:02}", "name": "core.bin"}
                                              for number in range(0, 32, 2)])
        first = await page_resources(db, {"deletedAt": None}, 1, 10, has_coredumps=None, sort_by="tasks", sort_order="desc")
        second = await page_resources(db, {"deletedAt": None}, 2, 10, has_coredumps=None, sort_by="tasks", sort_order="desc")
        assert not ({item["id"] for item in first["items"]} & {item["id"] for item in second["items"]})
        assert first["items"][0]["taskCount"] == 4 and next(item for item in first["items"] if item["id"] == "resource-31")["taskCount"] == 4
        present = await page_resources(db, {"deletedAt": None}, 1, 100, has_coredumps=True, sort_by="tasks", sort_order="desc")
        assert len(present["items"]) == 16 and all(item["hasCoredumps"] for item in present["items"])
        creator_first = await page_resources(db, {"deletedAt": None}, 1, 10, has_coredumps=None, sort_by="creator", sort_order="asc")
        creator_second = await page_resources(db, {"deletedAt": None}, 2, 10, has_coredumps=None, sort_by="creator", sort_order="asc")
        assert creator_first["items"][0]["id"] == "resource-31" and creator_second["items"][0]["id"] == "resource-21"
        await db.resources.update_one({"id": "resource-30"}, {"$set": {"deletedAt": "2026-09-10T12:00:00Z"}})
        await db.tasks.update_one({"id": "task-30-0"}, {"$set": {"resourceDeleted": True}})
        task_page = await _page_tasks(lambda: SimpleNamespace(db=db), {}, 1, 10, "resourceDeletedAt", "desc")
        assert task_page["items"][0]["id"] == "task-30-0"
        explain = await db.command("explain", {"aggregate": "resources", "pipeline": resource_list_pipeline(
            {"deletedAt": None}, 1, 10, has_coredumps=None, sort_by="tasks", sort_order="desc"), "cursor": {}}, verbosity="executionStats")
        assert "$_tasks" not in str(explain) and "$group" in str(explain)
        print({"database": database, "resourcePages": [len(first["items"]), len(second["items"])], "coredumps": len(present["items"]), "taskFirst": task_page["items"][0]["id"]})
    finally:
        await client.drop_database(database)
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
