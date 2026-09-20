"""在真实 MongoDB 随机库验证海康资源批量创建的原子性与幂等性。"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

import httpx
from camera_logs.common import audited_mutations
from camera_logs.common.config import Settings
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError


def check(condition, message: str) -> None:
    """以不含连接配置或口令的中文信息终止不符合事务不变量的验证。"""
    if not condition:
        raise AssertionError(message)


def items(*addresses: str) -> dict:
    """构造仅供进程内模拟认证使用的网络设备批次，不输出其口令。"""
    return {"items": [
        {"name": f"验证设备-{index}", "kind": "HIKVISION_NETWORK", "ip": address,
         "username": "verify", "password": f"verify-secret-{index}"}
        for index, address in enumerate(addresses)
    ]}


async def counts(db, addresses: list[str]) -> dict[str, int]:
    """按本次地址读取所有应同行提交的集合，供失败路径证明零局部写入。"""
    resource_ids = [item["id"] async for item in db.resources.find({"ip": {"$in": addresses}}, {"id": 1})]
    return {
        "resources": len(resource_ids),
        "addresses": await db.resource_addresses.count_documents({"_id": {"$in": addresses}}),
        "authenticationRecords": await db.authentication_records.count_documents({"resourceId": {"$in": resource_ids}}),
        "audits": await db.audit.count_documents({"targetId": {"$in": resource_ids}, "action": "create_resource"}),
    }


async def verify_success_and_replay(api, repo, db, calls: list[str]) -> None:
    """成功批次必须同行创建四类记录；已确认同键重放不再次发起设备认证。"""
    body, key = items("192.0.2.101", "192.0.2.102"), "batch-success"
    response = await api.post("/api/v1/resources/batch", json=body, headers={"Idempotency-Key": key})
    check(response.status_code == 201, "全成功批量创建未返回 201")
    created = response.json()["items"]
    check([item["ip"] for item in created] == ["192.0.2.101", "192.0.2.102"], "成功响应未保留请求顺序")
    check(all("password" not in item and "passwordEncrypted" not in item for item in created), "成功响应泄露口令字段")
    check(await counts(db, [item["ip"] for item in created]) == {
        "resources": 2, "addresses": 2, "authenticationRecords": 2, "audits": 2,
    }, "成功批次没有完整同行提交资源、地址、认证历史和审计")
    stored = [item async for item in db.resources.find({"id": {"$in": [item["id"] for item in created]}})]
    passwords = {item["ip"]: item["password"] for item in body["items"]}
    check(all(repo.decrypt(item["passwordEncrypted"]) == passwords[item["ip"]] for item in stored),
          "成功批次未逐项加密保存对应设备口令")
    check(await db.idempotency.count_documents({"actor": "bootstrap", "key": key}) == 1, "成功批次未写入唯一幂等映射")

    before = len(calls)
    replay = await api.post("/api/v1/resources/batch", json=body, headers={"Idempotency-Key": key})
    replay_items = replay.json().get("items", [])
    check(replay.status_code == 201 and [item["id"] for item in replay_items] == [item["id"] for item in created],
          "同键重放未返回原批次的稳定资源身份和顺序")
    check(len(calls) == before, "已确认同键重放重新认证设备")
    check(all("password" not in item and "passwordEncrypted" not in item for item in replay_items), "重放响应泄露口令字段")
    check("verify-secret" not in replay.text, "重放响应泄露设备口令")
    conflict = await api.post("/api/v1/resources/batch", json=items("192.0.2.103"),
                              headers={"Idempotency-Key": key})
    check(conflict.status_code == 409 and len(calls) == before, "同键不同请求未在认证前拒绝")


async def verify_authentication_failure(api, db) -> None:
    """任一设备认证失败时，整批不得写入，并必须按失败 IP 留下安全审计。"""
    addresses, key = ["192.0.2.141", "192.0.2.142"], "batch-auth-failure"
    response = await api.post("/api/v1/resources/batch", json=items(*addresses), headers={"Idempotency-Key": key})
    check(response.status_code == 422, "单项认证失败未返回 422")
    detail = response.json().get("error", {})
    check(detail.get("code") == "BATCH_AUTHENTICATION_FAILED" and detail.get("details") == {
        "items": [{"index": 1, "ip": addresses[1], "code": "AUTH_FAILED"}],
    }, "单项认证失败未返回限定的安全定位信息")
    check(await counts(db, addresses) == {
        "resources": 0, "addresses": 0, "authenticationRecords": 0, "audits": 0,
    }, "认证失败后仍写入了批量资源附属记录")
    check(await db.idempotency.count_documents({"key": key}) == 0, "认证失败保留了幂等映射")
    check(await db.audit.count_documents({
        "action": "authenticate_resource_credentials_rejected", "targetId": "ip:" + addresses[1],
    }) == 1, "认证失败审计未以失败 IP 作为目标")
    check("verify-secret" not in response.text, "认证失败响应泄露设备口令")


async def verify_conflict_rollback(api, db) -> None:
    """中间地址已占用时，事务必须撤销同批其它新资源及其所有附属记录。"""
    conflict = "192.0.2.112"
    await db.resources.insert_one({"id": "existing-address", "ip": conflict, "deletedAt": None})
    await db.resource_addresses.insert_one({"_id": conflict, "resourceId": "existing-address"})
    response = await api.post("/api/v1/resources/batch", json=items("192.0.2.111", conflict, "192.0.2.113"),
                              headers={"Idempotency-Key": "batch-ip-conflict"})
    check(response.status_code == 409, "中间 IP 冲突未返回 409")
    new_addresses = ["192.0.2.111", "192.0.2.113"]
    check(await counts(db, new_addresses) == {
        "resources": 0, "addresses": 0, "authenticationRecords": 0, "audits": 0,
    }, "IP 冲突后留下了同批局部写入")
    check((await db.resource_addresses.find_one({"_id": conflict}))["resourceId"] == "existing-address",
          "IP 冲突破坏既有地址占用")
    check(await db.idempotency.count_documents({"key": "batch-ip-conflict"}) == 0, "失败批次保留了幂等映射")


async def verify_audit_rollback(api, repo, db) -> None:
    """逐资源审计写入失败时，已插入的资源、历史、地址和幂等映射必须一并回滚。"""
    addresses, original = ["192.0.2.121", "192.0.2.122"], repo.audit

    async def fail_audit(*_args, **_kwargs):
        raise PyMongoError("injected batch audit failure")

    repo.audit = fail_audit
    try:
        response = await api.post("/api/v1/resources/batch", json=items(*addresses),
                                  headers={"Idempotency-Key": "batch-audit-failure"})
    finally:
        repo.audit = original
    check(response.status_code == 503, "审计失败未返回事务结果未知 503")
    check(await counts(db, addresses) == {
        "resources": 0, "addresses": 0, "authenticationRecords": 0, "audits": 0,
    }, "审计失败后留下了批量局部写入")
    check(await db.idempotency.count_documents({"key": "batch-audit-failure"}) == 0, "审计失败保留了幂等映射")


async def verify_concurrent_same_key(api, db) -> None:
    """同一幂等键并发请求至多提交一批，并且两个成功响应引用同一组资源。"""
    body, key = items("192.0.2.131", "192.0.2.132"), "batch-concurrent"
    first, second = await asyncio.gather(
        api.post("/api/v1/resources/batch", json=body, headers={"Idempotency-Key": key}),
        api.post("/api/v1/resources/batch", json=body, headers={"Idempotency-Key": key}),
    )
    check([response.status_code for response in (first, second)] == [201, 201], "同键并发未全部确认同一成功批次")
    first_ids = [item["id"] for item in first.json()["items"]]
    second_ids = [item["id"] for item in second.json()["items"]]
    check(first_ids == second_ids and len(first_ids) == 2, "同键并发返回了不同批次")
    check(await counts(db, ["192.0.2.131", "192.0.2.132"]) == {
        "resources": 2, "addresses": 2, "authenticationRecords": 2, "audits": 2,
    }, "同键并发提交了重复资源或附属记录")
    check(await db.idempotency.count_documents({"actor": "bootstrap", "key": key}) == 1, "同键并发产生多个幂等映射")


async def verify_unknown_commit_recovery(api, db, calls: list[str]) -> None:
    """事务已提交但 ACK 丢失时，仅从同键映射恢复结果，不能二次认证或审计。"""
    body, key = items("192.0.2.151", "192.0.2.152"), "batch-ack-lost"
    original, transactions = audited_mutations.mutation_transaction, 0

    async def committed_then_lost(current_repo, callback):
        nonlocal transactions
        transactions += 1
        await original(current_repo, callback)
        raise PyMongoError("injected batch commit acknowledgement loss")

    audited_mutations.mutation_transaction = committed_then_lost
    try:
        response = await api.post("/api/v1/resources/batch", json=body, headers={"Idempotency-Key": key})
    finally:
        audited_mutations.mutation_transaction = original
    check(response.status_code == 201 and transactions == 1, "提交确认丢失未恢复已确认批次")
    created = response.json()["items"]
    check(await counts(db, [item["ip"] for item in created]) == {
        "resources": 2, "addresses": 2, "authenticationRecords": 2, "audits": 2,
    }, "提交确认丢失后批次未完整提交或重复审计")
    before = len(calls)
    replay = await api.post("/api/v1/resources/batch", json=body, headers={"Idempotency-Key": key})
    check(replay.status_code == 201 and len(calls) == before, "确认丢失恢复后的重放重新认证")
    check([item["id"] for item in replay.json()["items"]] == [item["id"] for item in created],
          "确认丢失恢复后的重放未指向原批次")


async def main() -> None:
    """在本机现有 Mongo 配置上创建随机库；无论通过或失败均删除库和临时文件。"""
    configured, database_name, token = Settings(), "resource_batch_verify_" + uuid4().hex, uuid4().hex
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    temporary = TemporaryDirectory(prefix="resource-batch-")
    directory = Path(temporary.name)
    removed = False
    try:
        settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=database_name,
                            bootstrap_token=token, encryption_key=Fernet.generate_key().decode(), admin_password="",
                            start_background=False, log_root=directory / "logs")
        app = create_app(settings, mongo[database_name])
        async with app.router.lifespan_context(app):
            repo, calls = app.state.repo, []

            async def authenticated(**kwargs):
                calls.append(kwargs["ip"])
                if kwargs["ip"] == "192.0.2.142":
                    raise PermissionError("simulated authentication rejection")
                return {"model": "verify-model", "subSerialNumber": "verify-" + kwargs["ip"], "softwareVersion": "V"}

            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://verify", headers={
                "Authorization": "Bearer " + token,
            }) as api:
                with patch("camera_logs.resources.batch_creation.authenticate_network_resource", authenticated):
                    await verify_success_and_replay(api, repo, repo.db, calls)
                    await verify_authentication_failure(api, repo.db)
                    await verify_conflict_rollback(api, repo.db)
                    await verify_audit_rollback(api, repo, repo.db)
                    await verify_concurrent_same_key(api, repo.db)
                    await verify_unknown_commit_recovery(api, repo.db, calls)
    finally:
        await mongo.drop_database(database_name)
        removed = database_name not in await mongo.list_database_names()
        await mongo.close()
        temporary.cleanup()
    check(removed and not directory.exists(), "随机验证库或临时目录未清理")
    print(json.dumps({"passed": True, "successTransaction": True, "ipConflictRollback": True,
                      "auditFailureRollback": True, "sameKeyConcurrentSingleBatch": True,
                      "replayWithoutAuthentication": True, "unknownCommitRecovered": True,
                      "passwordsEncrypted": True, "authenticationFailureAuditedByIp": True, "responsesRedacted": True,
                      "noDeviceAccess": True, "temporaryDataRemoved": True}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
