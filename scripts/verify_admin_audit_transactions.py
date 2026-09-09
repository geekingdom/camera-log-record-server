"""以正式管理 API 验证配置、节点和来源白名单审计事务。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from camera_logs.access_policy.policy import POLICY_ID
from camera_logs.common.config import Settings
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

SOURCE = "198.51.100.10"
DENIED_SOURCE = "203.0.113.10"


def check(condition, message):
    """以中文断言说明失败的不变量，供本机和 CI 直接定位。"""
    if not condition:
        raise AssertionError(message)


def admin_headers(token):
    """只在内存中构造临时 bootstrap 凭据，绝不记录认证材料。"""
    return {"Authorization": f"Bearer {token}"}


def policy_body(version, *, scopes=None):
    """构造只表达平台客户端来源规则的有效白名单请求。"""
    return {
        "version": version,
        "enabled": True,
        "rules": [{
            "label": "temporary-admin-source",
            "network": "198.51.100.0/24",
            "scopes": scopes or ["admin"],
        }],
    }


def changed_policy_body(version):
    """构造改变来源匹配集合的候选规则，用于证明失败事务不会提前发布边界。"""
    return {
        "version": version,
        "enabled": True,
        "rules": [
            {"label": "only-current-source", "network": f"{SOURCE}/32", "scopes": ["admin", "tasks:read"]},
            {"label": "newly-allowed-source", "network": f"{DENIED_SOURCE}/32", "scopes": ["admin"]},
        ],
    }


async def request(client, method, path, expected, **kwargs):
    """调用正式 ASGI 路由并将意外状态带入断言，不打印敏感配置。"""
    response = await client.request(method, path, **kwargs)
    check(response.status_code == expected, f"{method} {path} 返回 {response.status_code}，预期 {expected}")
    return response


async def audit_count(repo, action, target):
    """按动作和业务目标隔离审计计数，排除初始化及其他验证步骤。"""
    return await repo.db.audit.count_documents({"action": action, "targetId": target})


async def with_audit_failure(repo, operation):
    """仅让审计插入抛真实驱动异常，业务路由仍使用正式事务 helper。"""
    original = repo.audit

    async def failed_audit(*_args, **_kwargs):
        raise PyMongoError("injected audit write failure")

    repo.audit = failed_audit
    try:
        return await operation()
    finally:
        repo.audit = original


async def verify_platform_settings(client, repo, headers):
    """验证保留期在审计失败、重试、陈旧版本和并发 CAS 下的原子性。"""
    before = await repo.db.platform_settings.find_one({"id": "platform"})
    check(before is None, "临时数据库在平台配置首次写入前已有默认记录")
    failed = await with_audit_failure(repo, lambda: client.patch(
        "/api/v1/platform-settings", headers=headers,
        json={"retentionDays": 14, "version": 1},
    ))
    check(failed.status_code == 503, "保留期审计失败没有返回 503")
    stored = await repo.db.platform_settings.find_one({"id": "platform"})
    check(stored is None, "保留期首次写入审计失败后仍留下默认配置或业务配置")
    check(await audit_count(repo, "update_platform_settings", "platform") == 0,
          "保留期审计失败后留下成功审计")

    retried = await request(client, "PATCH", "/api/v1/platform-settings", 200, headers=headers,
                            json={"retentionDays": 14, "version": 1})
    version = retried.json()["version"]
    check(await audit_count(repo, "update_platform_settings", "platform") == 1,
          "保留期重试没有留下恰好一条审计")
    await request(client, "PATCH", "/api/v1/platform-settings", 409, headers=headers,
                  json={"retentionDays": 15, "version": 1})
    check(await audit_count(repo, "update_platform_settings", "platform") == 1,
          "保留期陈旧版本追加了虚假审计")

    responses = await asyncio.gather(*[
        client.patch("/api/v1/platform-settings", headers=headers,
                     json={"retentionDays": 16 + index, "version": version})
        for index in range(4)
    ])
    check(sum(item.status_code == 200 for item in responses) == 1,
          "相同保留期版本并发请求没有恰好一个成功")
    check(sum(item.status_code == 409 for item in responses) == 3,
          "保留期并发失败请求没有全部返回 409")
    check(await audit_count(repo, "update_platform_settings", "platform") == 2,
          "保留期并发 CAS 产生了虚假审计")


async def register_node(client, headers, identifier):
    """通过正式登记路由建立独立节点夹具，不写入 worker 心跳或设备数据。"""
    response = await request(client, "POST", "/api/v1/admin/nodes", 201, headers=headers, json={
        "id": identifier, "url": f"https://{identifier}.example.test", "capacity": 8,
    })
    return response.json()


async def verify_nodes(client, repo, headers):
    """验证节点登记和节点配置更新均与对应审计在一个事务中完成。"""
    failed_id = "audit-transaction-failed-node"
    failed = await with_audit_failure(repo, lambda: client.post("/api/v1/admin/nodes", headers=headers, json={
        "id": failed_id, "url": "https://failed-node.example.test", "capacity": 8,
    }))
    check(failed.status_code == 503, "节点登记审计失败没有返回 503")
    check(await repo.db.node_configs.find_one({"id": failed_id}) is None,
          "节点登记审计失败后仍插入节点配置")
    check(await audit_count(repo, "register_node", failed_id) == 0,
          "节点登记审计失败后留下成功审计")
    await register_node(client, headers, failed_id)
    check(await audit_count(repo, "register_node", failed_id) == 1,
          "节点登记重试没有恰好一条审计")

    duplicate_id = "audit-transaction-concurrent-register"
    registration = {"id": duplicate_id, "url": "https://concurrent-register.example.test", "capacity": 8}
    responses = await asyncio.gather(*[client.post("/api/v1/admin/nodes", headers=headers, json=registration) for _ in range(4)])
    check(sum(item.status_code == 201 for item in responses) == 1,
          "并发节点登记没有恰好一个成功")
    check(sum(item.status_code == 409 for item in responses) == 3,
          "并发节点登记失败请求没有全部返回 409")
    check(await audit_count(repo, "register_node", duplicate_id) == 1,
          "并发节点登记产生了虚假审计")

    update_id = "audit-transaction-node-update"
    registered = await register_node(client, headers, update_id)
    failed = await with_audit_failure(repo, lambda: client.patch(
        f"/api/v1/admin/nodes/{update_id}", headers=headers,
        json={"version": registered["version"], "capacity": 9},
    ))
    check(failed.status_code == 503, "节点更新审计失败没有返回 503")
    stored = await repo.db.node_configs.find_one({"id": update_id})
    check(stored["capacity"] == 8 and stored["version"] == 1,
          "节点更新审计失败后业务配置或版本仍被提交")
    check(await audit_count(repo, "update_node_config", update_id) == 0,
          "节点更新审计失败后留下成功审计")

    updated = await request(client, "PATCH", f"/api/v1/admin/nodes/{update_id}", 200, headers=headers,
                            json={"version": 1, "capacity": 9})
    check(await audit_count(repo, "update_node_config", update_id) == 1,
          "节点更新重试没有恰好一条审计")
    await request(client, "PATCH", f"/api/v1/admin/nodes/{update_id}", 409, headers=headers,
                  json={"version": 1, "capacity": 10})
    check(await audit_count(repo, "update_node_config", update_id) == 1,
          "节点陈旧版本追加了虚假审计")

    version = updated.json()["version"]
    responses = await asyncio.gather(*[
        client.patch(f"/api/v1/admin/nodes/{update_id}", headers=headers,
                     json={"version": version, "capacity": 11 + index})
        for index in range(4)
    ])
    check(sum(item.status_code == 200 for item in responses) == 1,
          "相同节点版本并发请求没有恰好一个成功")
    check(sum(item.status_code == 409 for item in responses) == 3,
          "节点并发失败请求没有全部返回 409")
    check(await audit_count(repo, "update_node_config", update_id) == 2,
          "节点并发 CAS 产生了虚假审计")


async def verify_ip_policy(app, repo, database, token):
    """验证来源规则更新回滚且既有允许/拒绝边界不会被失败请求改变。"""
    headers = admin_headers(token)
    transport = httpx.ASGITransport(app=app, client=(SOURCE, 45000), raise_app_exceptions=False)
    denied_transport = httpx.ASGITransport(app=app, client=(DENIED_SOURCE, 45001), raise_app_exceptions=False)
    previous_transport = httpx.ASGITransport(app=app, client=("198.51.100.11", 45002), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://verify") as allowed, \
            httpx.AsyncClient(transport=denied_transport, base_url="http://verify") as denied, \
            httpx.AsyncClient(transport=previous_transport, base_url="http://verify") as previously_allowed:
        check(set(policy_body(1)) == {"version", "enabled", "rules"},
              "来源规则请求意外包含设备资源或设备地址字段")
        check(await database.ip_policy.find_one({"id": POLICY_ID}) is None,
              "临时数据库在 IP 规则首次写入前已有默认策略")
        failed = await with_audit_failure(repo, lambda: allowed.patch(
            "/api/v1/admin/ip-policy", headers=headers, json=policy_body(1),
        ))
        check(failed.status_code == 503, "IP 规则首次写入审计失败没有返回 503")
        check(await database.ip_policy.find_one({"id": POLICY_ID}) is None,
              "IP 规则首次写入审计失败后仍留下默认策略或业务策略")
        check(await audit_count(repo, "update_ip_policy", POLICY_ID) == 0,
              "IP 规则首次写入审计失败后留下成功审计")

        enabled = await request(allowed, "PATCH", "/api/v1/admin/ip-policy", 200, headers=headers,
                                json=policy_body(1))
        version = enabled.json()["version"]
        check((await denied.get("/api/v1/admin/ip-policy", headers=headers)).status_code == 403,
              "启用来源规则后不匹配来源没有被拒绝")
        check((await previously_allowed.get("/api/v1/admin/ip-policy", headers=headers)).status_code == 200,
              "初始来源规则没有允许同网段的既有来源")

        before = await database.ip_policy.find_one({"id": POLICY_ID})
        failed = await with_audit_failure(repo, lambda: allowed.patch(
            "/api/v1/admin/ip-policy", headers=headers,
            json=changed_policy_body(version),
        ))
        check(failed.status_code == 503, "IP 规则审计失败没有返回 503")
        stored = await database.ip_policy.find_one({"id": POLICY_ID})
        check(stored["version"] == before["version"] and stored["rules"] == before["rules"],
              "IP 规则审计失败后策略或版本仍被提交")
        check((await allowed.get("/api/v1/admin/ip-policy", headers=headers)).status_code == 200,
              "IP 规则失败后原本允许来源被意外阻断")
        check((await denied.get("/api/v1/admin/ip-policy", headers=headers)).status_code == 403,
              "IP 规则失败后原本拒绝来源被意外放开")
        check((await previously_allowed.get("/api/v1/admin/ip-policy", headers=headers)).status_code == 200,
              "IP 规则失败后原本允许来源被意外阻断")
        check(await audit_count(repo, "update_ip_policy", POLICY_ID) == 1,
              "IP 规则审计失败后留下成功审计")

        retried = await request(allowed, "PATCH", "/api/v1/admin/ip-policy", 200, headers=headers,
                                json=changed_policy_body(version))
        version = retried.json()["version"]
        check((await denied.get("/api/v1/admin/ip-policy", headers=headers)).status_code == 200,
              "IP 规则成功重试后新允许来源没有生效")
        check((await previously_allowed.get("/api/v1/admin/ip-policy", headers=headers)).status_code == 403,
              "IP 规则成功重试后旧来源匹配集合没有收缩")
        check(await audit_count(repo, "update_ip_policy", POLICY_ID) == 2,
              "IP 规则重试没有恰好一条审计")
        await request(allowed, "PATCH", "/api/v1/admin/ip-policy", 409, headers=headers,
                      json=policy_body(version - 1))
        unsafe = {"version": version, "enabled": True, "rules": [{
            "label": "self-lock", "network": "203.0.113.0/24", "scopes": ["admin"],
        }]}
        await request(allowed, "PATCH", "/api/v1/admin/ip-policy", 422, headers=headers, json=unsafe)
        check(await audit_count(repo, "update_ip_policy", POLICY_ID) == 2,
              "IP 规则陈旧版本或自锁拒绝追加了虚假审计")

        responses = await asyncio.gather(*[
            allowed.patch("/api/v1/admin/ip-policy", headers=headers,
                          json=policy_body(version, scopes=["admin", "tasks:read", "tasks:create"]))
            for _ in range(4)
        ])
        check(sum(item.status_code == 200 for item in responses) == 1,
              "相同 IP 规则版本并发请求没有恰好一个成功")
        check(sum(item.status_code == 409 for item in responses) == 3,
              "IP 规则并发失败请求没有全部返回 409")
        check(await audit_count(repo, "update_ip_policy", POLICY_ID) == 3,
              "IP 规则并发 CAS 产生了虚假审计")


async def main():
    """使用真实副本集随机库和临时日志目录，finally 无条件清理所有验证资源。"""
    configured = Settings()
    database_name = f"admin_audit_tx_verify_{uuid4().hex}"
    token = uuid4().hex
    check(database_name != configured.database_name, "验证脚本不能使用配置的主数据库")
    summary = None
    temporary_logs = TemporaryDirectory(prefix="admin-audit-transactions-")
    try:
        settings = Settings(
            _env_file=None,
            mongo_uri=configured.mongo_uri,
            database_name=database_name,
            bootstrap_token=token,
            encryption_key=Fernet.generate_key().decode(),
            admin_password="",
            start_background=False,
            log_root=Path(temporary_logs.name) / "logs",
        )
        client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                                  w="majority", journal=True)
        try:
            database = client[database_name]
            app = create_app(settings, database)
            async with app.router.lifespan_context(app):
                repo = app.state.repo
                headers = admin_headers(token)
                transport = httpx.ASGITransport(app=app, client=(SOURCE, 44999), raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport, base_url="http://verify") as api:
                    await verify_platform_settings(api, repo, headers)
                    await verify_nodes(api, repo, headers)
                await verify_ip_policy(app, repo, database, token)
            summary = {
                "passed": True,
                "realMongoReplicaSet": True,
                "formalFastApiRoutes": True,
                "auditFailureRollback": True,
                "retrySingleAudit": True,
                "concurrentCas": True,
                "staleAndSelfLockNoAudit": True,
                "ipSourceBoundary": True,
                "noDeviceAccess": True,
            }
        finally:
            try:
                await client.drop_database(database_name)
            finally:
                await client.close()
    finally:
        temporary_logs.cleanup()
    print(json.dumps(summary | {"temporaryDatabaseDropped": True, "temporaryLogDirectoryDropped": True}))


if __name__ == "__main__":
    asyncio.run(main())
