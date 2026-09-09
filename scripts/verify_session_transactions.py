"""以随机真实 MongoDB 库和正式 ASGI 路由验证登录、退出与改密会话事务。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from camera_logs.common import audited_mutations
from camera_logs.common.config import Settings
from camera_logs.main import create_app
from camera_logs.users import api as users_api
from camera_logs.users.passwords import verify_password
from cryptography.fernet import Fernet
from fastapi import HTTPException
from pymongo import AsyncMongoClient
from pymongo.errors import ConnectionFailure, PyMongoError

CSRF = {"X-Requested-With": "XMLHttpRequest"}


def check(condition, message):
    """使用不含令牌或密码的中文断言说明验证失败原因。"""
    if not condition:
        raise AssertionError(message)


def no_cookie(response, message):
    """事务未确认时响应不得把未持久化令牌写回浏览器。"""
    check("set-cookie" not in response.headers, message)


async def request(client, method, path, expected, **kwargs):
    """通过正式 ASGI API 请求并验证稳定的预期 HTTP 状态。"""
    response = await client.request(method, path, **kwargs)
    check(response.status_code == expected, f"{method} {path} 返回 {response.status_code}，预期 {expected}")
    return response


async def login(client, password):
    """登录固定临时管理员并由 httpx cookie jar 保存浏览器会话。"""
    return await request(client, "POST", "/api/v1/auth/login", 200, headers=CSRF,
                         json={"username": "admin", "password": password})


async def audit_failure(repo, call):
    """在事务内审计插入点注入真实驱动错误，随后无条件恢复仓储。"""
    original = repo.audit

    async def failed(*_args, **_kwargs):
        raise PyMongoError("injected session audit failure")

    repo.audit = failed
    try:
        return await call()
    finally:
        repo.audit = original


async def audit_cancelled(repo, call):
    """在审计插入处取消请求，路由包装状态不影响数据库回滚断言。"""
    original = repo.audit

    async def cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    repo.audit = cancelled
    try:
        return await call()
    finally:
        repo.audit = original


class SessionsProxy:
    """仅拒绝新会话 insert，其余 collection 操作原样走真实副本集。"""
    def __init__(self, collection):
        self.collection = collection

    async def insert_one(self, *_args, **_kwargs):
        raise PyMongoError("injected session insert failure")

    def __getattr__(self, name):
        return getattr(self.collection, name)


class DatabaseProxy:
    """保留真实 client 事务能力，只替换 user_sessions 集合的写入故障点。"""
    def __init__(self, database):
        self.database, self.client = database, database.client

    def __getattr__(self, name):
        return SessionsProxy(self.database.user_sessions) if name == "user_sessions" else getattr(self.database, name)

    def __getitem__(self, name):
        return SessionsProxy(self.database.user_sessions) if name == "user_sessions" else self.database[name]


async def assert_session(client, expected, message):
    """以 /me 验证浏览器持有的会话实际可用性或已撤销状态。"""
    await request(client, "GET", "/api/v1/auth/me", expected)


async def session_audit_counts(database, user_id):
    """读取本次用户的会话和三种审计数，便于精确比对失败回滚。"""
    return {
        "sessions": await database.user_sessions.count_documents({"userId": user_id}),
        "login": await database.audit.count_documents({"action": "login", "targetId": user_id}),
        "logout": await database.audit.count_documents({"action": "logout", "targetId": user_id}),
        "password": await database.audit.count_documents({"action": "change_password", "targetId": user_id}),
    }


async def verify_routes(client, repo, database):
    """覆盖会话轮换、审计回滚、密码原子性与登录并发账号变化。"""
    old_password, next_password = "initial-admin-password", "next-admin-password-123"
    await login(client, old_password)
    user = await database.users.find_one({"username": "admin"})
    old_sessions = await database.user_sessions.count_documents({"userId": user["id"]})
    before_counts = await session_audit_counts(database, user["id"])
    failed = await audit_failure(repo, lambda: client.post("/api/v1/auth/login", headers=CSRF,
        json={"username": "admin", "password": old_password}))
    check(failed.status_code == 503, "登录审计失败没有返回 503")
    no_cookie(failed, "登录审计失败仍写入 Set-Cookie")
    check(await database.user_sessions.count_documents({"userId": user["id"]}) == old_sessions,
          "登录审计失败改变旧会话或创建新会话")
    check(await session_audit_counts(database, user["id"]) == before_counts, "登录审计失败新增审计")
    await assert_session(client, 200, "登录审计失败后旧会话失效")

    await login(client, old_password)
    check(await database.user_sessions.count_documents({"userId": user["id"]}) == 1, "成功登录没有轮换为唯一会话")

    before_counts = await session_audit_counts(database, user["id"])
    original = repo.db
    repo.db = DatabaseProxy(database)
    try:
        failed = await client.post("/api/v1/auth/login", headers=CSRF,
                                   json={"username": "admin", "password": old_password})
    finally:
        repo.db = original
    check(failed.status_code == 503, "登录新会话写失败没有返回 503")
    no_cookie(failed, "登录新会话写失败仍写入 Set-Cookie")
    check(await session_audit_counts(database, user["id"]) == before_counts, "登录新会话写失败留下会话或审计")
    await assert_session(client, 200, "登录新会话写失败撤销了旧会话")

    before_counts = await session_audit_counts(database, user["id"])
    failed = await audit_failure(repo, lambda: client.post("/api/v1/auth/logout", headers=CSRF))
    check(failed.status_code == 503, "退出审计失败没有返回 503")
    no_cookie(failed, "退出审计失败仍写入 Set-Cookie")
    check(await session_audit_counts(database, user["id"]) == before_counts, "退出审计失败新增审计")
    await assert_session(client, 200, "退出审计失败撤销了旧会话")
    await request(client, "POST", "/api/v1/auth/logout", 204, headers=CSRF)
    await assert_session(client, 401, "成功退出没有撤销会话")

    await login(client, old_password)
    before = await database.users.find_one({"id": user["id"]})
    before_counts = await session_audit_counts(database, user["id"])
    failed = await audit_failure(repo, lambda: client.post("/api/v1/auth/password", headers=CSRF,
        json={"currentPassword": old_password, "newPassword": next_password}))
    check(failed.status_code == 503, "改密审计失败没有返回 503")
    no_cookie(failed, "改密审计失败仍写入 Set-Cookie")
    stored = await database.users.find_one({"id": user["id"]})
    check(stored["authVersion"] == before["authVersion"] and verify_password(old_password, stored["passwordHash"]),
          "改密审计失败改变密码或认证版本")
    check(await session_audit_counts(database, user["id"]) == before_counts, "改密审计失败新增审计")
    await assert_session(client, 200, "改密审计失败撤销旧会话")

    before_counts = await session_audit_counts(database, user["id"])
    original = repo.db
    repo.db = DatabaseProxy(database)
    try:
        failed = await client.post("/api/v1/auth/password", headers=CSRF,
            json={"currentPassword": old_password, "newPassword": next_password})
    finally:
        repo.db = original
    check(failed.status_code == 503, "新会话写失败没有返回 503")
    no_cookie(failed, "新会话写失败仍写入 Set-Cookie")
    stored = await database.users.find_one({"id": user["id"]})
    check(verify_password(old_password, stored["passwordHash"]), "新会话写失败仍提交新密码")
    check(await session_audit_counts(database, user["id"]) == before_counts, "改密新会话写失败新增审计")
    await assert_session(client, 200, "新会话写失败撤销了旧会话")

    changed = await request(client, "POST", "/api/v1/auth/password", 200, headers=CSRF,
                            json={"currentPassword": old_password, "newPassword": next_password})
    check(changed.json()["user"]["mustChangePassword"] is False, "成功改密未返回新认证状态")
    await assert_session(client, 200, "成功改密签发的新会话不可用")

    # 来源策略拒绝必须发生在任何会话、密码或审计写入之前。
    original_enforce = users_api.enforce_ip

    async def denied_policy(*_args, **_kwargs):
        raise HTTPException(403, "injected policy rejection")

    users_api.enforce_ip = denied_policy
    try:
        for path, body in (
            ("/api/v1/auth/login", {"username": "admin", "password": next_password}),
            ("/api/v1/auth/password", {"currentPassword": next_password, "newPassword": "policy-password-123"}),
        ):
            before_counts = await session_audit_counts(database, user["id"])
            before_user = await database.users.find_one({"id": user["id"]})
            denied = await client.post(path, headers=CSRF, json=body)
            check(denied.status_code == 403, "来源策略拒绝没有返回 403")
            no_cookie(denied, "来源策略拒绝仍写入 Set-Cookie")
            check(await session_audit_counts(database, user["id"]) == before_counts, "来源策略拒绝留下会话或审计")
            check(await database.users.find_one({"id": user["id"]}) == before_user, "来源策略拒绝改变密码或账号")
    finally:
        users_api.enforce_ip = original_enforce

    # 鉴权后读取账户前发生 authVersion 变化，原会话与新读到的版本不匹配，改密必须 409。
    original_get, changed_version = repo.get, False

    async def advance_before_get(collection, identifier):
        nonlocal changed_version
        if collection == "users" and identifier == user["id"] and not changed_version:
            changed_version = True
            await database.users.update_one({"id": user["id"]}, {"$inc": {"authVersion": 1}})
        return await original_get(collection, identifier)

    before_counts = await session_audit_counts(database, user["id"])
    password_before_race = await database.users.find_one({"id": user["id"]})
    repo.get = advance_before_get
    try:
        stale = await client.post("/api/v1/auth/password", headers=CSRF,
                                  json={"currentPassword": next_password, "newPassword": "stale-password-123"})
    finally:
        repo.get = original_get
    check(stale.status_code == 409 and changed_version, "账号版本先变化时改密没有返回 409")
    no_cookie(stale, "账号版本先变化的改密仍写入 Set-Cookie")
    stored = await database.users.find_one({"id": user["id"]})
    check(verify_password(next_password, stored["passwordHash"]), "账号版本先变化时改密改变了密码")
    check(await session_audit_counts(database, user["id"]) == before_counts, "账号版本先变化时改密新增审计")
    check(stored["authVersion"] == password_before_race["authVersion"] + 1, "外部认证版本变化未保留")

    # 上个场景刻意使旧会话失效；随机库内清除限流计数，只为后续独立事务场景准备新会话。
    await database.login_limits.delete_many({})
    await login(client, next_password)
    original_work, revoked = users_api.password_work, False

    async def revoke_after_verify(current_repo, function, *args):
        nonlocal revoked
        result = await original_work(current_repo, function, *args)
        if function is verify_password and not revoked:
            revoked = True
            await database.user_sessions.delete_many({"userId": user["id"]})
        return result

    before_counts = await session_audit_counts(database, user["id"])
    before_user = await database.users.find_one({"id": user["id"]})
    users_api.password_work = revoke_after_verify
    try:
        revoked_response = await client.post("/api/v1/auth/password", headers=CSRF,
                                             json={"currentPassword": next_password, "newPassword": "revoked-password-123"})
    finally:
        users_api.password_work = original_work
    check(revoked_response.status_code == 409 and revoked, "验密后会话撤销时改密没有返回 409")
    no_cookie(revoked_response, "验密后会话撤销的改密仍写入 Set-Cookie")
    stored = await database.users.find_one({"id": user["id"]})
    check(verify_password(next_password, stored["passwordHash"]), "验密后会话撤销仍改变密码")
    check((await session_audit_counts(database, user["id"]))["password"] == before_counts["password"],
          "验密后会话撤销新增改密审计")
    check(stored == before_user, "验密后会话撤销改变账号状态")

    await login(client, next_password)

    # 审计插入处取消时，无论 ASGI 将其包装为何种失败状态，数据库必须完整回滚。
    for path, body in (
        ("/api/v1/auth/login", {"username": "admin", "password": next_password}),
        ("/api/v1/auth/logout", None),
        ("/api/v1/auth/password", {"currentPassword": next_password, "newPassword": "cancelled-password-123"}),
    ):
        before_counts = await session_audit_counts(database, user["id"])
        before_user = await database.users.find_one({"id": user["id"]})
        response = await audit_cancelled(repo, lambda path=path, body=body: client.post(path, headers=CSRF, json=body))
        check(response.status_code >= 500, "审计取消没有返回失败状态")
        no_cookie(response, "审计取消仍写入 Set-Cookie")
        check(await session_audit_counts(database, user["id"]) == before_counts, "审计取消留下会话或审计")
        check(await database.users.find_one({"id": user["id"]}) == before_user, "审计取消改变账号状态")

    # 事务 helper 在回调开始前报告驱动错误时，路由只能返回 503 且不得留下写入。
    original_transaction = audited_mutations.mutation_transaction

    async def unavailable(_repo, _callback):
        raise PyMongoError("injected transaction startup failure")

    before_counts = await session_audit_counts(database, user["id"])
    audited_mutations.mutation_transaction = unavailable
    try:
        failed = await client.post("/api/v1/auth/login", headers=CSRF,
                                   json={"username": "admin", "password": next_password})
    finally:
        audited_mutations.mutation_transaction = original_transaction
    check(failed.status_code == 503, "事务启动前驱动错误没有返回 503")
    no_cookie(failed, "事务启动前驱动错误仍写入 Set-Cookie")
    check(await session_audit_counts(database, user["id"]) == before_counts, "事务启动前驱动错误留下写入")

    # 在读取账号后、事务 CAS 前真实改写账号，验证登录不会凭过期读取签发会话或审计。
    for mode in ("disable", "auth-version"):
        original_work, injected = users_api.password_work, False

        async def change_after_verify(current_repo, function, *args, _mode=mode, _work=original_work):
            nonlocal injected
            result = await _work(current_repo, function, *args)
            if function is verify_password and not injected:
                injected = True
                updates = {"enabled": False} if _mode == "disable" else {
                    "authVersion": (await database.users.find_one({"id": user["id"]}))["authVersion"] + 1}
                await database.users.update_one({"id": user["id"]}, {"$set": updates})
            return result

        users_api.password_work = change_after_verify
        other = httpx.AsyncClient(transport=client._transport, base_url="http://verify")
        before_counts = await session_audit_counts(database, user["id"])
        try:
            response = await other.post("/api/v1/auth/login", headers=CSRF, json={"username": "admin", "password": next_password})
            check(response.status_code in {401, 409}, "账号停用或认证版本变更后登录未拒绝")
            no_cookie(response, "账号变化拒绝登录仍写入 Set-Cookie")
        finally:
            await other.aclose()
            users_api.password_work = original_work
        await database.users.update_one({"id": user["id"]}, {"$set": {"enabled": True}})
        after_counts = await session_audit_counts(database, user["id"])
        check(after_counts == before_counts, "账号变化拒绝登录仍新增会话或审计")

    before_audits = await database.audit.count_documents({"action": "login", "targetId": user["id"]})
    async def committed_then_lost(current_repo, callback):
        await original_transaction(current_repo, callback)
        raise ConnectionFailure("injected commit acknowledgement loss")
    audited_mutations.mutation_transaction = committed_then_lost
    try:
        recovered = await request(client, "POST", "/api/v1/auth/login", 200, headers=CSRF,
                                  json={"username": "admin", "password": next_password})
    finally:
        audited_mutations.mutation_transaction = original_transaction
    check(recovered.status_code == 200 and await database.audit.count_documents({"action": "login", "targetId": user["id"]}) == before_audits + 1,
          "提交 ACK 丢失后的只读恢复失败或重复审计")
    final_password = "final-admin-password-123"
    before_password_audits = await database.audit.count_documents({"action": "change_password", "targetId": user["id"]})
    audited_mutations.mutation_transaction = committed_then_lost
    try:
        await request(client, "POST", "/api/v1/auth/password", 200, headers=CSRF,
                      json={"currentPassword": next_password, "newPassword": final_password})
    finally:
        audited_mutations.mutation_transaction = original_transaction
    stored = await database.users.find_one({"id": user["id"]})
    check(verify_password(final_password, stored["passwordHash"]), "改密提交 ACK 丢失未恢复新密码")
    check(await database.audit.count_documents({"action": "change_password", "targetId": user["id"]}) == before_password_audits + 1,
          "改密提交 ACK 丢失后的只读恢复重复审计")
    await assert_session(client, 200, "改密提交 ACK 丢失恢复的新会话不可用")

    before_logouts = await database.audit.count_documents({"action": "logout", "targetId": user["id"]})
    audited_mutations.mutation_transaction = committed_then_lost
    try:
        await request(client, "POST", "/api/v1/auth/logout", 204, headers=CSRF)
    finally:
        audited_mutations.mutation_transaction = original_transaction
    check(await database.audit.count_documents({"action": "logout", "targetId": user["id"]}) == before_logouts + 1,
          "退出提交 ACK 丢失后的只读恢复重复审计")
    await assert_session(client, 401, "退出提交 ACK 丢失后会话没有撤销")


async def main():
    """创建随机数据库和临时日志；finally 删除二者，绝不访问主库或设备。"""
    configured, name = Settings(), f"session_tx_verify_{uuid4().hex}"
    check(name != configured.database_name, "验证脚本不能使用主数据库")
    temporary_logs = TemporaryDirectory(prefix="session-transactions-")
    temporary_log_path = Path(temporary_logs.name)
    database_dropped = temporary_logs_dropped = False
    try:
        settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=name,
            bootstrap_token=uuid4().hex, encryption_key=Fernet.generate_key().decode(), admin_username="admin",
            admin_password="initial-admin-password", start_background=False, log_root=Path(temporary_logs.name) / "logs")
        mongo = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
        try:
            database, app = mongo[name], create_app(settings, mongo[name])
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app, client=("198.51.100.9", 40000), raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://verify") as client:
                    await verify_routes(client, app.state.repo, database)
            hello = await database.command("hello")
            result = {"passed": bool(hello.get("setName")), "realMongoReplicaSet": bool(hello.get("setName")), "formalFastApiRoutes": True,
                      "sessionAndAuditAtomic": True, "commitRecoveryReadOnly": True}
        finally:
            try:
                await mongo.drop_database(name)
                database_dropped = name not in await mongo.list_database_names()
            finally:
                await mongo.close()
    finally:
        temporary_logs.cleanup()
        temporary_logs_dropped = not temporary_log_path.exists()
    print(json.dumps(result | {"temporaryDatabaseDropped": database_dropped,
                               "temporaryLogDirectoryDropped": temporary_logs_dropped}))


if __name__ == "__main__":
    asyncio.run(main())
