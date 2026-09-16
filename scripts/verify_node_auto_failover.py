"""验证双 Worker 自动故障转移，只使用回环 SSH 源、随机本机 Mongo 与临时日志。

本脚本让仍可访问的 A Worker 心跳过期，再由调度器调用其内部精确关闭接口。它验证
关闭回执、旧 run/端点锁收尾和 B Worker 的新 run；关闭后台 scheduler 后由脚本确定性
编排同一围栏、回执消费和领取函数，不连接实体设备，也不模拟网络分区。
"""

import argparse
import asyncio
import json
import secrets
import sys
import tempfile
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.common.node_http import NodeHttpPool
from camera_logs.node.failover import (
    AUTO_FAILOVER_FENCED,
    consume_confirmed_failovers,
    request_reachable_worker_fencing,
)
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from verify_cross_worker_ssh_resume import (
    INITIAL_COMMANDS,
    DeviceInfoSource,
    SshSource,
    environment,
    free_port,
    local_mongo_uri,
    operation,
    stop,
    wait_ready,
    wait_task,
)

ROOT = Path(__file__).resolve().parents[1]


async def wait_files(database, task_id, node_id):
    """等待 Worker 关闭时将实际会话日志发布为可读取归档。"""
    observed = []
    try:
        async with asyncio.timeout(20):
            while True:
                observed = [item async for item in database.files.find({"taskId": task_id, "nodeId": node_id})]
                ready = [item for item in observed if item.get("status") == "READY"]
                if ready and all(Path(item["path"]).is_file() for item in ready):
                    return ready
                await asyncio.sleep(.2)
    except TimeoutError as error:
        details = [{key: item.get(key) for key in ("runId", "status", "path", "archiveName", "archiveError")} for item in observed]
        raise RuntimeError(f"节点 {node_id} 归档未就绪: {details!r}") from error


async def wait_node_accepting(database, node_id):
    """等待 Worker 将正式管理配置写入本节点心跳，避免使用过期候选快照。"""
    async with asyncio.timeout(20):
        while True:
            node = await database.nodes.find_one({"id": node_id})
            if node and node.get("accepting") is True:
                return
            await asyncio.sleep(.2)


async def main(isapi_test_port=80):
    """执行 A 自动围栏、B 新运行领取及两次归档的真实本机验证。"""
    configured = Settings()
    if not local_mongo_uri(configured.mongo_uri):
        raise RuntimeError("本验证仅允许本机回环 MongoDB")
    database = f"node_auto_failover_{uuid4().hex}"
    token, key, password = secrets.token_urlsafe(32), Fernet.generate_key().decode(), secrets.token_urlsafe(24)
    api_port, a_port, b_port = free_port(), free_port(), free_port()
    processes, logs, failed = [], [], True
    source, device = SshSource(password), DeviceInfoSource(password, isapi_test_port)
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    with tempfile.TemporaryDirectory(prefix="camera-node-auto-failover-") as temporary:
        base = Path(temporary)
        try:
            await device.start()
            await source.start()
            for node_id, port in (("failover-a", a_port), ("failover-b", b_port)):
                log = (base / f"{node_id}.log").open("w")
                logs.append(log)
                processes.append(await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "camera_logs.worker", cwd=ROOT,
                    env=environment(database, configured.mongo_uri, base / node_id, node_id, port, token, key, password),
                    stdout=log, stderr=log,
                ))
            api_log = (base / "api.log").open("w")
            logs.append(api_log)
            # 调度由本脚本在关键状态后显式执行，避免后台周期与心跳刷新制造竞态。
            api_env = environment(database, configured.mongo_uri, base / "api", "failover-api", api_port, token, key, password,
                                  start_background=False)
            if isapi_test_port != 80:
                api_env["ISAPI_TEST_PORT"] = str(isapi_test_port)
            processes.append(await asyncio.create_subprocess_exec(
                sys.executable, "-m", "scripts.cross_worker_ssh_resume_api", cwd=ROOT, env=api_env,
                stdout=api_log, stderr=api_log,
            ))
            await asyncio.gather(*(wait_ready(f"http://127.0.0.1:{port}/health", processes)
                                   for port in (api_port, a_port, b_port)))
            headers = {"Authorization": "Bearer " + token}
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}", headers=headers, timeout=15) as client:
                for node_id, port, accepting in (("failover-a", a_port, True), ("failover-b", b_port, False)):
                    response = await client.post("/api/v1/admin/nodes", json={
                        "id": node_id, "url": f"http://127.0.0.1:{port}", "capacity": 10, "accepting": accepting,
                    })
                    response.raise_for_status()
                resource = await client.post("/api/v1/resources", headers={**headers, "Idempotency-Key": "resource-" + uuid4().hex}, json={
                    "name": "自动故障转移回环源", "kind": "HIKVISION_NETWORK", "ip": "127.0.0.1",
                    "username": "collector", "password": password, "authType": "BASIC",
                })
                resource.raise_for_status()
                created = await client.post("/api/v1/tasks", headers={**headers, "Idempotency-Key": "task-" + uuid4().hex}, json={
                    "name": "自动故障转移 SSH", "description": "", "sharedWith": [], "sharedWithAll": False,
                    "protocol": "SSH", "ip": "127.0.0.1", "port": source.port, "username": "collector",
                    "password": password, "resourceId": resource.json()["id"], "autoStart": True,
                    "initialCommands": [{"command": item} for item in INITIAL_COMMANDS],
                })
                created.raise_for_status()
                task_id = created.json()["id"]
                verifier_settings = Settings(
                    _env_file=None, mongo_uri=configured.mongo_uri, database_name=database, internal_token=token,
                    node_id="failover-verifier", node_url="http://127.0.0.1:1", log_root=base / "verifier",
                    encryption_key=key, start_background=False,
                )
                repo = Repository(mongo[database], verifier_settings)
                async with NodeHttpPool() as pool:
                    repo.node_http = pool
                    await schedule_once(repo)
                first = await wait_task(client, task_id, lambda task: task.get("status") == "COLLECTING" and task.get("nodeId") == "failover-a", "A 未开始采集")
                await source.wait_commands(1, expect_probe=False)
                await source.suspend_after(1)
                nodes = (await client.get("/api/v1/admin/nodes")).json()["items"]
                b_config = next(item for item in nodes if item["id"] == "failover-b")
                changed = await client.patch(f"/api/v1/admin/nodes/{b_config['id']}", json={
                    "version": b_config["version"], "accepting": True,
                })
                changed.raise_for_status()
                await wait_node_accepting(mongo[database], "failover-b")
                # 独立调度周期在 A 仍可访问时看到过期心跳，必须先取得该 Worker 的关闭证明。
                # 写入紧邻实际调度，不能给 A 的每秒 tick 覆盖旧心跳的窗口。
                await mongo[database].nodes.update_one({"id": "failover-a"}, {"$set": {"heartbeat": now() - timedelta(minutes=5)}})
                stale_a = await mongo[database].nodes.find_one({"id": "failover-a"})
                async with NodeHttpPool() as pool:
                    repo.node_http = pool
                    if await request_reachable_worker_fencing(repo, [stale_a]) != 1:
                        raise RuntimeError("旧 Worker 未返回精确自围栏关闭收据")
                    if await consume_confirmed_failovers(repo) != 1:
                        raise RuntimeError("精确自围栏收据未释放旧运行")
                    await schedule_once(repo)
                second = await wait_task(client, task_id, lambda task: task.get("status") == "COLLECTING" and task.get("nodeId") == "failover-b", "B 未自动接管")
                if second["runId"] == first["runId"] or source.active != 1 or source.connection_count != 2:
                    raise RuntimeError("自动迁移没有形成唯一 B 连接及新的运行")
                await source.wait_commands(2, expect_probe=False)
                old = await mongo[database].runs.find_one({"id": first["runId"], "taskId": task_id})
                if not old or not old.get("endedAt") or await mongo[database].endpoint_locks.find_one({"taskId": task_id, "runId": first["runId"]}):
                    raise RuntimeError("旧运行或端点锁未收尾")
                old_files = await wait_files(mongo[database], task_id, "failover-a")
                event = await mongo[database].events.find_one({"taskId": task_id, "type": "TASK_AUTO_MIGRATED"})
                if not event or event.get("reason") != AUTO_FAILOVER_FENCED:
                    raise RuntimeError("缺少自动迁移审计事件")
                source.resume_emitting()
                await source.suspend_after(2)
                stopped = await client.post(f"/api/v1/tasks/{task_id}/stop")
                stopped.raise_for_status()
                await operation(client, stopped.json()["id"])
                await asyncio.wait_for(source.closed.wait(), 15)
                new_files = await wait_files(mongo[database], task_id, "failover-b")
                if any(item["runId"] != first["runId"] for item in old_files) or any(item["runId"] != second["runId"] for item in new_files):
                    raise RuntimeError("归档文件运行归属错误")
                if source.peak_active > 1 or any(not Path(item["path"]).is_relative_to(base / "failover-a") for item in old_files) \
                        or any(not Path(item["path"]).is_relative_to(base / "failover-b") for item in new_files):
                    raise RuntimeError("自动迁移发生重叠连接或归档落入错误节点根目录")
                print(json.dumps({"passed": True, "oldRunId": first["runId"], "newRunId": second["runId"],
                                  "activeConnections": source.active, "peakActiveConnections": source.peak_active,
                                  "connections": source.connection_count,
                                  "oldArchives": [item["path"] for item in old_files], "newArchives": [item["path"] for item in new_files]}, ensure_ascii=False))
                failed = False
        finally:
            results = await asyncio.gather(*(stop(process) for process in reversed(processes)), return_exceptions=True)
            for log in logs:
                if failed:
                    log.flush()
                    tail = Path(log.name).read_text(errors="replace")[-3000:]
                    if tail:
                        print(f"{Path(log.name).stem} tail:\n{tail}")
                log.close()
            await source.close()
            await device.close()
            await mongo.drop_database(database)
            await mongo.close()
            if any(isinstance(result, BaseException) for result in results):
                raise RuntimeError("隔离子进程未能结束")
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryLogRootsDropped": 3, "failed": failed}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isapi-test-port", type=int, default=80)
    arguments = parser.parse_args()
    if not 1 <= arguments.isapi_test_port <= 65535:
        raise ValueError("--isapi-test-port 必须在1至65535")
    asyncio.run(main(arguments.isapi_test_port))
