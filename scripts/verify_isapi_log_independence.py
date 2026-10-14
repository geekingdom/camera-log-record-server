"""真实回环 SSH、HTTP 与 Mongo 副本集验证 ISAPI 异常不抢停有效采集。

只操作随机数据库和临时目录；API 创建任务，真实 Worker 持续采集，认证调用真实
HTTP 响应。脚本显式驱动认证时机，避免等待生产的分钟级认证周期。
"""

import argparse
import asyncio
import json
import secrets
import sys
import tarfile
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.resources.health import check_resource
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from verify_cross_worker_ssh_resume import (
    ROOT,
    DeviceInfoSource,
    SshSource,
    environment,
    free_port,
    local_mongo_uri,
    stop,
    wait_ready,
    wait_task,
)
from verify_node_auto_failover import wait_files, wait_node_accepting


async def main(isapi_port=80):
    """验证 HTTP 401/503 时会话保持，以及停止输出后及时停止并归档。"""
    configured = Settings()
    if not local_mongo_uri(configured.mongo_uri):
        raise RuntimeError("只允许回环 MongoDB 验证")
    name = "isapi_log_independence_" + uuid4().hex
    token, key, password = secrets.token_urlsafe(32), Fernet.generate_key().decode(), secrets.token_urlsafe(24)
    api_port, worker_port, node_id = free_port(), free_port(), "independence-worker"
    source, device = SshSource(password), DeviceInfoSource(password, isapi_port)
    mongo = AsyncMongoClient(configured.mongo_uri, tz_aware=True, w="majority", journal=True)
    processes, logs, failed = [], [], True
    with tempfile.TemporaryDirectory(prefix="camera-isapi-independence-") as temporary:
        root = Path(temporary)
        try:
            await source.start()
            await device.start()
            for role, port, module in ((node_id, worker_port, "camera_logs.worker"),
                                       ("independence-api", api_port, "scripts.cross_worker_ssh_resume_api")):
                log = (root / f"{role}.log").open("w")
                logs.append(log)
                env = environment(name, configured.mongo_uri, root / role, role, port, token, key, password,
                                  start_background=False)
                if isapi_port != 80:
                    env["ISAPI_TEST_PORT"] = str(isapi_port)
                processes.append(await asyncio.create_subprocess_exec(
                    sys.executable, "-m", module, cwd=ROOT, env=env, stdout=log, stderr=log,
                ))
            await asyncio.gather(*(wait_ready(f"http://127.0.0.1:{port}/health", processes)
                                   for port in (worker_port, api_port)))
            repo = Repository(mongo[name], Settings(
                _env_file=None, mongo_uri=configured.mongo_uri, database_name=name, encryption_key=key,
                internal_token=token, log_root=root / "verifier", start_background=False,
            ))
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}",
                                         headers={"Authorization": "Bearer " + token}, timeout=20) as client:
                response = await client.post("/api/v1/admin/nodes", json={
                    "id": node_id, "url": f"http://127.0.0.1:{worker_port}", "capacity": 10, "accepting": True,
                })
                response.raise_for_status()
                response = await client.post("/api/v1/resources", headers={"Idempotency-Key": uuid4().hex}, json={
                    "name": "ISAPI与采集独立回环设备", "kind": "HIKVISION_NETWORK", "ip": "127.0.0.1",
                    "username": "collector", "password": password, "authType": "BASIC",
                })
                response.raise_for_status()
                resource_id = response.json()["id"]
                response = await client.post("/api/v1/tasks", headers={"Idempotency-Key": uuid4().hex}, json={
                    "name": "ISAPI异常持续日志", "protocol": "SSH", "resourceId": resource_id,
                    "ip": "127.0.0.1", "port": source.port, "username": "collector", "password": password,
                    "autoStart": True, "initialCommands": [], "scheduledCommands": [],
                })
                response.raise_for_status()
                task_id = response.json()["id"]
                await wait_node_accepting(mongo[name], node_id)
                await schedule_once(repo)
                first = await wait_task(client, task_id, lambda task: task["status"] == "COLLECTING", "未开始采集")
                await asyncio.sleep(1)
                observed = []
                # 只适配端口，HTTP解析、事务和Worker逻辑均使用生产函数。
                with patch("camera_logs.resources.authentication._device_info_url",
                           return_value=f"http://127.0.0.1:{isapi_port}/ISAPI/System/deviceInfo"):
                    for status, expected in ((401, "AUTH_FAILED"), (503, "ERROR")):
                        device.response_status = status
                        before = len(source.sent)
                        await check_resource(repo, await repo.db.resources.find_one({"id": resource_id}))
                        await asyncio.sleep(3)
                        current = (await client.get(f"/api/v1/tasks/{task_id}")).json()
                        resource = await repo.db.resources.find_one({"id": resource_id})
                        assert resource["healthStatus"] == expected
                        assert current["desiredState"] == "RUNNING" and current["status"] == "COLLECTING"
                        assert (current["runId"], current["sessionId"]) == (first["runId"], first["sessionId"])
                        assert len(source.sent) > before and source.connection_count == 1
                        assert not current.get("resourceHealthRecovery")
                        observed.append(expected)
                    # HTTP服务真正停止监听，日志连接仍持续输出。
                    await device.close()
                    device.server, device.thread = None, None
                    await check_resource(repo, await repo.db.resources.find_one({"id": resource_id}))
                    await asyncio.sleep(3)
                    current = (await client.get(f"/api/v1/tasks/{task_id}")).json()
                    assert current["desiredState"] == "RUNNING" and current["sessionId"] == first["sessionId"]
                    assert (await repo.db.resources.find_one({"id": resource_id}))["healthStatus"] == "OFFLINE"
                    observed.append("OFFLINE")
                # 将下次HTTP检查置于一天之后，证明无输出停止不依赖下一次认证。
                await repo.db.resources.update_one({"id": resource_id},
                                                   {"$set": {"nextHealthCheckAt": now() + timedelta(days=1)}})
                source.silence_device()
                stopped = await wait_task(client, task_id,
                                          lambda task: task["desiredState"] == "STOPPED" and task["status"] == "STOPPED",
                                          "HTTP异常且持续无输出未停止")
                assert stopped["resourceHealthRecovery"]["reason"] == "OFFLINE"
                await asyncio.wait_for(source.closed.wait(), 10)
                files = await wait_files(mongo[name], task_id, node_id)
                assert files and all(item["bytes"] > 0 for item in files)
                recovered = []
                for path in sorted({item["path"] for item in files}):
                    with tarfile.open(path, "r:gz") as archive:
                        for member in archive.getmembers():
                            if member.isfile():
                                with archive.extractfile(member) as stream:
                                    recovered.extend(line.split(b"] ", 1)[1] for line in stream)
                assert recovered == source.sent, "归档正文与源端发送顺序/内容不一致"
                assert await repo.db.audit.count_documents({"action": "resource_health_changed:OFFLINE"}) == 1
                print(json.dumps({"passed": True, "preservedStates": observed, "sameSessionPreserved": True,
                                  "stoppedWithoutNextHttpProbe": True, "archives": len(files),
                                  "verifiedLogLines": len(recovered)}, ensure_ascii=False))
                failed = False
        finally:
            results = await asyncio.gather(*(stop(process) for process in reversed(processes)), return_exceptions=True)
            for log in logs:
                if failed:
                    log.flush()
                    print(Path(log.name).read_text(errors="replace")[-3000:])
                log.close()
            await source.close()
            await device.close()
            await mongo.drop_database(name)
            await mongo.close()
            if any(isinstance(result, BaseException) for result in results):
                raise RuntimeError("隔离进程收尾失败")
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryLogsDropped": True}))


async def verify_auth_reconnect_latch(isapi_port=80):
    """验证 401 锁存阻止 SSH 重连，只有 SUCCESS 放行且用户停止不会被重新唤醒。"""
    configured = Settings()
    if not local_mongo_uri(configured.mongo_uri):
        raise RuntimeError("只允许回环 MongoDB 验证")
    name = "isapi_auth_reconnect_" + uuid4().hex
    token, key, password = secrets.token_urlsafe(32), Fernet.generate_key().decode(), secrets.token_urlsafe(24)
    api_port, worker_port, node_id = free_port(), free_port(), "auth-reconnect-worker"
    source, device = SshSource(password), DeviceInfoSource(password, isapi_port)
    mongo = AsyncMongoClient(configured.mongo_uri, tz_aware=True, w="majority", journal=True)
    processes, logs, failed = [], [], True
    with tempfile.TemporaryDirectory(prefix="camera-isapi-auth-reconnect-") as temporary:
        root = Path(temporary)
        try:
            await source.start()
            await device.start()
            for role, port, module in ((node_id, worker_port, "camera_logs.worker"),
                                       ("auth-reconnect-api", api_port, "scripts.cross_worker_ssh_resume_api")):
                log = (root / f"{role}.log").open("w")
                logs.append(log)
                env = environment(name, configured.mongo_uri, root / role, role, port, token, key, password,
                                  start_background=False)
                if isapi_port != 80:
                    env["ISAPI_TEST_PORT"] = str(isapi_port)
                processes.append(await asyncio.create_subprocess_exec(
                    sys.executable, "-m", module, cwd=ROOT, env=env, stdout=log, stderr=log,
                ))
            await asyncio.gather(*(wait_ready(f"http://127.0.0.1:{port}/health", processes)
                                   for port in (worker_port, api_port)))
            repo = Repository(mongo[name], Settings(
                _env_file=None, mongo_uri=configured.mongo_uri, database_name=name, encryption_key=key,
                internal_token=token, log_root=root / "verifier", start_background=False,
            ))
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}",
                                         headers={"Authorization": "Bearer " + token}, timeout=20) as client:
                response = await client.post("/api/v1/admin/nodes", json={
                    "id": node_id, "url": f"http://127.0.0.1:{worker_port}", "capacity": 10, "accepting": True,
                })
                response.raise_for_status()
                response = await client.post("/api/v1/resources", headers={"Idempotency-Key": uuid4().hex}, json={
                    "name": "401重连锁存回环设备", "kind": "HIKVISION_NETWORK", "ip": "127.0.0.1",
                    "username": "collector", "password": password, "authType": "BASIC",
                })
                response.raise_for_status()
                resource_id = response.json()["id"]
                response = await client.post("/api/v1/tasks", headers={"Idempotency-Key": uuid4().hex}, json={
                    "name": "401断线后认证恢复", "protocol": "SSH", "resourceId": resource_id,
                    "ip": "127.0.0.1", "port": source.port, "username": "collector", "password": password,
                    "autoStart": True, "initialCommands": [], "scheduledCommands": [],
                })
                response.raise_for_status()
                task_id = response.json()["id"]
                await wait_node_accepting(mongo[name], node_id)
                await schedule_once(repo)
                first = await wait_task(client, task_id, lambda task: task["status"] == "COLLECTING", "未开始采集")
                with patch("camera_logs.resources.authentication._device_info_url",
                           return_value=f"http://127.0.0.1:{isapi_port}/ISAPI/System/deviceInfo"):
                    device.response_status = 401
                    await check_resource(repo, await repo.db.resources.find_one({"id": resource_id}))
                    rejected = await repo.db.resources.find_one({"id": resource_id})
                    assert rejected["healthStatus"] == "AUTH_FAILED" and rejected.get("authRejectedAt") is not None
                    lines_before_disconnect = len(source.sent)
                    await asyncio.sleep(1)
                    live_after_401 = (await client.get(f"/api/v1/tasks/{task_id}")).json()
                    assert live_after_401["status"] == "COLLECTING"
                    assert source.connection_count == 1 and len(source.sent) > lines_before_disconnect
                    await source.disconnect_active()
                    await asyncio.wait_for(source.closed.wait(), 10)
                    reconnecting = await wait_task(
                        client, task_id, lambda task: task["status"] == "RECONNECTING", "断线后未进入重连等待",
                    )
                    assert reconnecting["desiredState"] == "RUNNING"
                    await asyncio.sleep(3)
                    assert source.connection_count == 1, "401 锁存后仍尝试 SSH 登录"

                    device.response_status = 500
                    await check_resource(repo, await repo.db.resources.find_one({"id": resource_id}))
                    errored = await repo.db.resources.find_one({"id": resource_id})
                    assert errored["healthStatus"] == "ERROR"
                    assert errored.get("authRejectedAt") == rejected["authRejectedAt"]
                    await asyncio.sleep(2)
                    assert source.connection_count == 1, "ERROR 错误清除了 401 登录锁存"

                    device.response_status = 200
                    await check_resource(repo, await repo.db.resources.find_one({"id": resource_id}))
                    recovered_resource = await repo.db.resources.find_one({"id": resource_id})
                    assert recovered_resource["healthStatus"] == "ONLINE"
                    assert "authRejectedAt" not in recovered_resource
                    recovered = await wait_task(
                        client, task_id,
                        lambda task: task["status"] == "COLLECTING" and task.get("sessionId") != first["sessionId"],
                        "SUCCESS 后没有自动恢复 SSH 采集",
                    )
                    assert recovered["desiredState"] == "RUNNING" and source.connection_count == 2

                    device.response_status = 401
                    await check_resource(repo, await repo.db.resources.find_one({"id": resource_id}))
                    await source.disconnect_active()
                    await asyncio.wait_for(source.closed.wait(), 10)
                    await wait_task(client, task_id, lambda task: task["status"] == "RECONNECTING",
                                    "第二次断线后未进入重连等待")
                    stopped = await client.post(f"/api/v1/tasks/{task_id}/stop")
                    stopped.raise_for_status()
                    await wait_task(
                        client, task_id,
                        lambda task: task["desiredState"] == "STOPPED" and task["status"] == "STOPPED",
                        "用户停止未完成",
                    )
                    device.response_status = 200
                    await check_resource(repo, await repo.db.resources.find_one({"id": resource_id}))
                    await asyncio.sleep(3)
                    final = (await client.get(f"/api/v1/tasks/{task_id}")).json()
                    assert (final["desiredState"], final["status"]) == ("STOPPED", "STOPPED")
                    assert source.connection_count == 2, "用户停止后 SUCCESS 重新建立了 SSH 会话"
                print(json.dumps({"passed": True, "realMongoReplicaSet": True, "loopbackSsh": True,
                                  "authFailedPreservesLiveCollection": True,
                                  "authRejectedBlocksReconnect": True, "errorDoesNotClearLatch": True,
                                  "successAutomaticallyRecovers": True, "userStopDoesNotRevive": True,
                                  "connections": source.connection_count}, ensure_ascii=False))
                failed = False
        finally:
            results = await asyncio.gather(*(stop(process) for process in reversed(processes)), return_exceptions=True)
            for log in logs:
                if failed:
                    log.flush()
                    print(Path(log.name).read_text(errors="replace")[-3000:])
                log.close()
            await source.close()
            await device.close()
            await mongo.drop_database(name)
            await mongo.close()
            if any(isinstance(result, BaseException) for result in results):
                raise RuntimeError("隔离进程收尾失败")
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryLogsDropped": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isapi-test-port", type=int, default=80)
    parser.add_argument("--auth-reconnect-latch", action="store_true",
                        help="验证 401 后断线重连门控、SUCCESS 放行及用户停止保护")
    arguments = parser.parse_args()
    asyncio.run(verify_auth_reconnect_latch(arguments.isapi_test_port)
                if arguments.auth_reconnect_latch else main(arguments.isapi_test_port))
