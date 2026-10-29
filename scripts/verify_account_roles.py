"""以真实 Mongo 副本集验证账户角色围栏及全员手工命令事务。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

CSRF = {"X-Requested-With": "XMLHttpRequest"}
BOOTSTRAP = {**CSRF, "Authorization": "Bearer verify-role-token"}


def check(value, message):
    """使用不含账户凭据的中文说明中止验证。"""
    if not value:
        raise AssertionError(message)


async def call(client, method, path, expected, **kwargs):
    """调用正式 ASGI 路由并检查稳定状态。"""
    response = await client.request(method, path, **kwargs)
    check(response.status_code == expected, f"{method} {path} 返回 {response.status_code}，预期 {expected}")
    return response


async def create_user(client, username, *, role="USER"):
    """使用超级管理员建立合成账户。"""
    return (await call(client, "POST", "/api/v1/users", 201, headers=BOOTSTRAP, json={
        "username": username, "displayName": username, "password": "verify-role-password",
        "role": role, "scopes": [],
    })).json()


async def login_ready(client, username):
    """完成首登改密并保留浏览器会话。"""
    await call(client, "POST", "/api/v1/auth/login", 200, headers=CSRF,
               json={"username": username, "password": "verify-role-password"})
    return (await call(client, "POST", "/api/v1/auth/password", 200, headers=CSRF, json={
        "currentPassword": "verify-role-password", "newPassword": "verify-role-password-next",
    })).json()["user"]


async def login_existing(client, username):
    """重新登录已完成首登改密的合成账户，以取得配置变更后的新会话。"""
    return (await call(client, "POST", "/api/v1/auth/login", 200, headers=CSRF,
                       json={"username": username, "password": "verify-role-password-next"})).json()["user"]


async def main():
    """执行竞争、回滚、管理员保护和命令幂等验证，并无条件回收随机库。"""
    uri, database_name = Settings().mongo_uri, f"verify_account_roles_{uuid4().hex}"
    mongo = AsyncMongoClient(uri)
    with TemporaryDirectory(prefix="camera-account-roles-") as temporary:
        settings = Settings(_env_file=None, mongo_uri=uri, database_name=database_name,
                            bootstrap_token="verify-role-token", encryption_key=Fernet.generate_key().decode(),
                            admin_password="", log_root=Path(temporary), start_background=False)
        app = create_app(settings, mongo[database_name])
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        try:
            async with app.router.lifespan_context(app), httpx.AsyncClient(transport=transport, base_url="http://verify") as bootstrap, httpx.AsyncClient(transport=transport, base_url="http://verify") as manager_client, httpx.AsyncClient(transport=transport, base_url="http://verify") as user_client:
                database = app.state.repo.db
                manager, target = await create_user(bootstrap, "verify-manager", role="ADMIN"), await create_user(bootstrap, "verify-target")
                await login_ready(manager_client, "verify-manager")

                # 暂停迟到管理员事务，在超级管理员降权后恢复，目标和审计均不能写入。
                from camera_logs.users import api as users_api
                reached, release, original_claim = asyncio.Event(), asyncio.Event(), users_api.claim_account_manager

                async def paused_claim(repository, identity, **kwargs):
                    if identity.get("id") == manager["id"]:
                        reached.set()
                        await release.wait()
                    return await original_claim(repository, identity, **kwargs)

                users_api.claim_account_manager = paused_claim
                try:
                    late = asyncio.create_task(manager_client.patch(f"/api/v1/users/{target['id']}", headers=CSRF, json={"version": target["version"], "displayName": "不得写入"}))
                    await asyncio.wait_for(reached.wait(), timeout=5)
                    current = await database.users.find_one({"id": manager["id"]})
                    await call(bootstrap, "PATCH", f"/api/v1/users/{manager['id']}", 200, headers=BOOTSTRAP, json={"version": current["version"], "role": "USER"})
                    release.set()
                    late_response = await late
                finally:
                    users_api.claim_account_manager = original_claim
                    release.set()
                check(late_response.status_code == 403, "降级后的迟到管理员更新未被事务围栏拒绝")
                unchanged = await database.users.find_one({"id": target["id"]})
                check(unchanged["displayName"] == target["displayName"] and unchanged["version"] == target["version"], "迟到管理员更新改变了目标")
                check(await database.audit.count_documents({"actor": manager["id"], "action": "edit_user", "targetId": target["id"]}) == 0, "迟到管理员更新留下审计")

                # 审计失败须回滚新账号；不能留下无审计的活动用户名。
                original_audit = app.state.repo.audit

                async def fail_audit(*_args, **_kwargs):
                    raise PyMongoError("injected account audit failure")

                app.state.repo.audit = fail_audit
                try:
                    failed = await bootstrap.post("/api/v1/users", headers=BOOTSTRAP, json={"username": "audit-rollback", "displayName": "audit-rollback", "password": "verify-role-password", "scopes": []})
                finally:
                    app.state.repo.audit = original_audit
                check(failed.status_code == 503 and await database.users.count_documents({"username": "audit-rollback"}) == 0, "创建账户审计失败未完整回滚")

                # 管理员账户和其令牌不能被另一个管理员编辑、删除、重置或借令牌绕过。
                protected = await create_user(bootstrap, "protected-manager", role="ADMIN")
                token = (await call(bootstrap, "POST", "/api/v1/service-tokens", 201, headers=BOOTSTRAP, json={"name": "protected-token", "userId": protected["id"]})).json()
                current = await database.users.find_one({"id": manager["id"]})
                await call(bootstrap, "PATCH", f"/api/v1/users/{manager['id']}", 200, headers=BOOTSTRAP, json={"version": current["version"], "role": "ADMIN"})
                await login_existing(manager_client, "verify-manager")
                version = (await database.users.find_one({"id": protected["id"]}))["version"]
                attempts = [
                    manager_client.patch(f"/api/v1/users/{protected['id']}", headers=CSRF, json={"version": version, "enabled": False}),
                    manager_client.post(f"/api/v1/users/{protected['id']}/reset-password", headers=CSRF, json={"version": version, "password": "another-password"}),
                    manager_client.delete(f"/api/v1/users/{protected['id']}", headers=CSRF, params={"version": version}),
                    manager_client.post("/api/v1/service-tokens", headers=CSRF, json={"name": "forbidden", "userId": protected["id"]}),
                    manager_client.post(f"/api/v1/service-tokens/{token['id']}/reveal", headers=CSRF),
                    manager_client.delete(f"/api/v1/service-tokens/{token['id']}", headers=CSRF),
                ]
                for response in await asyncio.gather(*attempts):
                    check(response.status_code == 403, "管理员未被阻止操作其他管理员账户或令牌")

                # 普通用户拥有固定 commands:send；跨创建人任务命令、审计与幂等映射必须同事务。
                sender = await create_user(bootstrap, "ordinary-sender")
                identity = await login_ready(user_client, "ordinary-sender")
                check("commands:send" in identity["scopes"], "普通用户未获得固定手工命令权限")
                await database.tasks.insert_one({"id": "foreign-command-task", "createdBy": "another-owner", "nodeId": "verify-node", "status": "COLLECTING", "desiredState": "RUNNING", "runId": "verify-run", "sessionId": "verify-session", "generation": 1, "deletedAt": None})
                command_body = {"command": "printf verify", "newline": "\n", "delaySeconds": 0, "prompt": None, "timeoutSeconds": 30}
                first = await call(user_client, "POST", "/api/v1/tasks/foreign-command-task/commands", 202, headers={**CSRF, "Idempotency-Key": "ordinary-command"}, json=command_body)
                second = await call(user_client, "POST", "/api/v1/tasks/foreign-command-task/commands", 202, headers={**CSRF, "Idempotency-Key": "ordinary-command"}, json=command_body)
                command_id = first.json()["id"]
                check(second.json()["id"] == command_id and await database.commands.count_documents({"id": command_id}) == 1, "同键手工命令重复入队")
                check(await database.audit.count_documents({"actor": sender["id"], "action": "command:foreign-command-task", "targetId": command_id}) == 1, "手工命令审计未保持一次")
                check(await database.idempotency.count_documents({"actor": sender["id"], "key": "ordinary-command", "resourceId": command_id}) == 1, "手工命令幂等映射未保持一次")
                print(json.dumps({"passed": True, "managerFence": True, "createAuditRollback": True, "adminProtection": True, "ordinaryManualCommand": True}, ensure_ascii=False))
        finally:
            await mongo.drop_database(database_name)
            check(database_name not in await mongo.list_database_names(), "随机验证数据库未删除")
            await mongo.close()


if __name__ == "__main__":
    asyncio.run(main())
