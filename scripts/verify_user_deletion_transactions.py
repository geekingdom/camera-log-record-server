"""以随机真实 Mongo 副本集库验证用户删除、资源处置和用户写入围栏。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from camera_logs.common.audited_mutations import audited_create
from camera_logs.common.config import Settings
from camera_logs.common.database import now
from camera_logs.main import create_app
from camera_logs.resources.address_claim import claim_address
from camera_logs.tasks.creation import create_task
from camera_logs.users.write_gate import claim_active_user_for_resource_write
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

CSRF = {"X-Requested-With": "XMLHttpRequest"}


def check(value, message):
    """以不包含凭据的中文说明中止失败的真实库验证。"""
    if not value:
        raise AssertionError(message)


async def call(client, method, path, expected, **kwargs):
    """调用正式 ASGI 路由并核对稳定 HTTP 状态。"""
    response = await client.request(method, path, **kwargs)
    check(response.status_code == expected, f"{method} {path} 返回 {response.status_code}，预期 {expected}")
    return response


async def create_user(client, name):
    """用系统令牌创建真实普通用户，供删除和转移场景使用。"""
    response = await call(client, "POST", "/api/v1/users", 201, headers={**CSRF, "Authorization": "Bearer verify-token"}, json={
        "username": name, "displayName": name, "password": "verify-password", "scopes": [],
    })
    return response.json()


async def main():
    """执行索引、回滚、并发和转移目标围栏检查，最后无条件删除随机库。"""
    mongo = AsyncMongoClient(Settings().mongo_uri)
    name = f"verify_user_deletion_{uuid4().hex}"
    with TemporaryDirectory(prefix="camera-user-delete-") as temporary:
        # 模拟升级前仅有全局用户名唯一索引的数据库；正式 lifespan 必须先建立
        # 局部唯一索引，再移除该旧索引，避免同名已删除账号阻塞重建。
        await mongo[name].users.create_index("username", unique=True, name="username_1")
        settings = Settings(_env_file=None, mongo_uri=Settings().mongo_uri, database_name=name,
                            bootstrap_token="verify-token", encryption_key=Fernet.generate_key().decode(),
                            admin_password="", log_root=Path(temporary), start_background=False)
        app = create_app(settings, mongo[name])
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        try:
            async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://verify") as client:
                owner, target, other = await create_user(client, "owner"), await create_user(client, "target"), await create_user(client, "other")
                database = app.state.repo.db
                await database.resources.insert_one({"id": "resource", "ip": "192.0.2.250", "kind": "SERIAL_SERVER", "version": 1,
                                                     "createdBy": owner["id"], "createdByName": owner["displayName"]})
                await database.resource_addresses.insert_one({"_id": "192.0.2.250", "resourceId": "resource"})
                await database.tasks.insert_many([
                    {"id": "move", "resourceId": "resource", "createdBy": owner["id"], "version": 1, "desiredState": "RUNNING", "status": "COLLECTING"},
                    {"id": "keep", "resourceId": "resource", "createdBy": other["id"], "version": 1, "desiredState": "RUNNING", "status": "COLLECTING"},
                ])
                missing = await client.delete(f"/api/v1/users/{owner['id']}?version=1", headers={**CSRF, "Authorization": "Bearer verify-token"})
                check(missing.status_code == 422, "活动资源缺少处置策略未拒绝")
                await call(client, "DELETE", f"/api/v1/users/{owner['id']}?version=1&resourceAction=TRANSFER&targetUserId={target['id']}", 204,
                           headers={**CSRF, "Authorization": "Bearer verify-token"})
                resource, moved, kept = await database.resources.find_one({"id": "resource"}), await database.tasks.find_one({"id": "move"}), await database.tasks.find_one({"id": "keep"})
                check(resource["createdBy"] == target["id"] and resource["version"] == 2, "资源转移或版本递增失败")
                check(moved["createdBy"] == target["id"] and kept["createdBy"] == other["id"], "任务转移越过原创建人边界")

                # 审计失败必须回滚用户和其资源，不能留下无审计的删除意图。
                rollback = await create_user(client, "rollback")
                await database.resources.insert_one({"id": "rollback-resource", "ip": "192.0.2.251", "kind": "SERIAL_SERVER", "version": 1,
                                                     "createdBy": rollback["id"]})
                original_audit = app.state.repo.audit
                async def fail_audit(*_args, **_kwargs):
                    raise PyMongoError("injected audit failure")
                app.state.repo.audit = fail_audit
                try:
                    response = await client.delete(f"/api/v1/users/{rollback['id']}?version=1&resourceAction=DELETE", headers={**CSRF, "Authorization": "Bearer verify-token"})
                    check(response.status_code == 503, "审计失败删除没有返回503")
                finally:
                    app.state.repo.audit = original_audit
                check((await database.users.find_one({"id": rollback["id"]})).get("deletedAt") is None, "审计失败仍删除用户")
                check((await database.resources.find_one({"id": "rollback-resource"})).get("deletedAt") is None, "审计失败仍删除资源")

                # 同名并发只允许一个活动用户名；部分唯一索引必须命中该查询。
                first, second = await asyncio.gather(
                    client.post("/api/v1/users", headers={**CSRF, "Authorization": "Bearer verify-token"}, json={"username": "same-name", "displayName": "一", "password": "verify-password", "scopes": []}),
                    client.post("/api/v1/users", headers={**CSRF, "Authorization": "Bearer verify-token"}, json={"username": "same-name", "displayName": "二", "password": "verify-password", "scopes": []}),
                )
                check(sorted((first.status_code, second.status_code)) == [201, 409], "同名并发没有恰好一个成功")
                explain = await database.command({"explain": {"find": "users", "filter": {"username": "same-name", "deletedAt": None}}})
                check("users_active_username" in json.dumps(explain, default=str), "活动用户名查询没有命中部分唯一索引")
                # 旧全局 username 索引必须在初始化后由部分唯一索引替换，已删除同名
                # 账户不会阻塞重建；该迁移约束只允许索引名和选项按既有规则演进。
                indexes = await database.users.index_information()
                check("username_1" not in indexes and indexes["users_active_username"].get("unique"), "旧用户名唯一索引未迁移")
                # 转移和目标删除竞争时，围栏要求二者至多一个提交；成功转移后目标删除
                # 因新资源而必须提供处置策略，成功删除后转移则不得写入已删除目标。
                race_owner, race_target = await create_user(client, "race-owner"), await create_user(client, "race-target")
                await database.resources.insert_one({"id": "race-resource", "ip": "192.0.2.252", "kind": "SERIAL_SERVER", "version": 1,
                                                     "createdBy": race_owner["id"]})
                transfer, remove_target = await asyncio.gather(
                    client.delete(f"/api/v1/users/{race_owner['id']}?version=1&resourceAction=TRANSFER&targetUserId={race_target['id']}", headers={**CSRF, "Authorization": "Bearer verify-token"}),
                    client.delete(f"/api/v1/users/{race_target['id']}?version=1", headers={**CSRF, "Authorization": "Bearer verify-token"}),
                )
                check(sorted((transfer.status_code, remove_target.status_code)) in ([204, 422], [409, 204]), "转移与目标删除竞争结果不受围栏保护")
                current_target, race_resource = await database.users.find_one({"id": race_target["id"]}), await database.resources.find_one({"id": "race-resource"})
                check(current_target.get("deletedAt") is None or race_resource.get("createdBy") != race_target["id"], "资源被转移到已删除目标")

                # 正式资源创建事务和用户删除竞争：若创建先提交，DELETE 必须随即
                # 软删该资源；若删除先提交，用户围栏让创建事务重试后拒绝。
                create_race = await create_user(client, "create-race")
                identity = {"id": create_race["id"], "displayName": create_race["displayName"], "scopes": ["*"]}
                async def prepare(identifier):
                    return {"id": identifier, "name": "race", "kind": "SERIAL_SERVER", "ip": "192.0.2.253", "version": 1,
                            "createdAt": now(), "updatedAt": now(), "createdBy": identity["id"], "createdByName": identity["displayName"]}
                async def reserve(document, session):
                    await claim_active_user_for_resource_write(database, identity["id"], session)
                    await claim_address(app.state.repo, document, session)
                _create_result, delete_result = await asyncio.gather(
                    audited_create(app.state.repo, identity["id"], "create-race", "create_resource", {"ip": "192.0.2.253"}, "resources", prepare, before_insert=reserve),
                    client.delete(f"/api/v1/users/{identity['id']}?version=1&resourceAction=DELETE", headers={**CSRF, "Authorization": "Bearer verify-token"}),
                    return_exceptions=True,
                )
                check(getattr(delete_result, "status_code", None) in {204, 422}, "资源创建删除竞争返回异常状态")
                check(await database.resources.count_documents({"createdBy": identity["id"], "deletedAt": None}) == 0, "删除用户后仍有活动创建资源")

                # 正式任务创建事务同样先领取用户围栏；资源删除后不得留下活动任务。
                task_race = await create_user(client, "task-race")
                await database.resources.insert_one({"id": "task-race-resource", "ip": "192.0.2.254", "kind": "SERIAL_SERVER", "version": 1,
                                                     "createdBy": task_race["id"], "createdByName": task_race["displayName"]})
                task_identity = {"id": task_race["id"], "displayName": task_race["displayName"], "scopes": ["*"]}
                async def task_prepare(identifier):
                    return ({"id": identifier, "name": "race-task", "resourceId": "task-race-resource", "protocol": "TELNET_SERIAL", "ip": "192.0.2.254", "port": 23, "version": 1, "scheduledCommands": []}, False)
                _task_result, task_delete = await asyncio.gather(
                    create_task(app.state.repo, task_identity, "task-race", {"resourceId": "task-race-resource"}, task_prepare),
                    client.delete(f"/api/v1/users/{task_identity['id']}?version=1&resourceAction=DELETE", headers={**CSRF, "Authorization": "Bearer verify-token"}),
                    return_exceptions=True,
                )
                check(getattr(task_delete, "status_code", None) in {204, 422}, "任务创建删除竞争返回异常状态")
                check(await database.tasks.count_documents({"createdBy": task_identity["id"], "deletedAt": None}) == 0, "删除用户后仍有活动创建任务")
                print(json.dumps({"passed": True, "transfer": True, "auditRollback": True, "duplicateUsername": True, "partialIndex": True, "targetDeleteRace": True, "resourceCreateDeleteRace": True, "taskCreateDeleteRace": True}))
        finally:
            await mongo.drop_database(name)
            check(name not in await mongo.list_database_names(), "随机验证数据库未删除")
            await mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
