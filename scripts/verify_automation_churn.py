"""真实Mongo及HTTP/WebSocket并发收尾验证；模拟节点只返回空日志，不访问设备。"""

import asyncio
import hashlib
import json
import secrets
import socket
import time
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
import uvicorn
import websockets
from camera_logs.common.config import Settings
from camera_logs.common.database import now
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pymongo import AsyncMongoClient
from verify_isolated_browser_smoke import local_mongo_uri


@asynccontextmanager
async def serve(app):
    """仅监听随机回环端口；退出时等待服务及全部连接回收。"""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, timeout_graceful_shutdown=5))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(30):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("隔离服务未完成启动")
                await asyncio.sleep(.02)
        yield port
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, 15)
        finally:
            sock.close()


async def api(client, method, path, expected=200, **kwargs):
    """正式HTTP路由校验，不输出响应正文或认证凭据。"""
    response = await client.request(method, path, **kwargs)
    if response.status_code != expected:
        raise AssertionError(f"{method} {path.split('?')[0]}: {response.status_code} != {expected}")
    return response.json()


def percentile(values, fraction):
    """最近秩分位，报告为本机短时端到端HTTP毫秒。"""
    return round(sorted(values)[min(len(values) - 1, int(len(values) * fraction))], 3)


async def exercise(app, port, token, calls, *, rounds=4, concurrency=12):
    """并行资源/任务CRUD、空日志订阅与列表查询；每轮证明断开后tail计数稳定。"""
    repo = app.state.repo
    latencies, results = [], []
    stop_queries = asyncio.Event()
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=15,
                                 headers={"Authorization": f"Bearer {token}"}) as client:
        async def query_resources():
            while not stop_queries.is_set():
                started = time.perf_counter()
                await api(client, "GET", "/api/v1/resources")
                latencies.append((time.perf_counter() - started) * 1000)
                await asyncio.sleep(.1)

        async def cycle(index, delete_while_subscribed):
            resource = await api(client, "POST", "/api/v1/resources", 201,
                                 headers={"Idempotency-Key": uuid4().hex}, json={
                                     "name": f"isolated-churn-{index}", "kind": "SERIAL_SERVER",
                                     "ip": f"127.1.{index // 250}.{index % 250 + 1}",
                                 })
            task = await api(client, "POST", "/api/v1/tasks", 201,
                             headers={"Idempotency-Key": uuid4().hex}, json={
                                 "name": f"isolated-churn-{index}", "resourceId": resource["id"],
                                 "protocol": "TELNET_SERIAL", "ip": resource["ip"], "port": 2323,
                             })
            identifier = task["id"]
            # 模拟节点归属只存在随机测试库，不产生设备连接/端点锁；真实Worker另行验收。
            await repo.db.tasks.update_one({"id": identifier}, {"$set": {
                "nodeId": "synthetic-tail", "status": "COLLECTING", "desiredState": "RUNNING",
            }})
            async with websockets.connect(f"ws://127.0.0.1:{port}/api/v1/tasks/{identifier}/logs") as ws:
                await ws.send(json.dumps({"token": token}))
                async with asyncio.timeout(10):
                    while calls[identifier] < 2:
                        await asyncio.sleep(.02)
                if delete_while_subscribed:
                    await api(client, "DELETE", f"/api/v1/tasks/{identifier}?version=1", 202)
                    async with asyncio.timeout(5):
                        await ws.wait_closed()
                    if ws.close_code != 1000:
                        raise AssertionError("软删除未正常关闭实时订阅")
            if not delete_while_subscribed:
                await api(client, "DELETE", f"/api/v1/tasks/{identifier}?version=1", 202)
            await api(client, "DELETE", f"/api/v1/resources/{resource['id']}?version=1", 202)
            return identifier

        query = asyncio.create_task(query_resources())
        try:
            for number in range(rounds):
                # 任一路失败必须先取消并等待所有同轮协程，之后才能关闭服务与删除随机库。
                async with asyncio.TaskGroup() as group:
                    cycles = [group.create_task(cycle(number * concurrency + i, i % 2 == 0))
                              for i in range(concurrency)]
                identifiers = [task.result() for task in cycles]
                await asyncio.sleep(.3)
                before = {key: calls[key] for key in identifiers}
                await asyncio.sleep(.5)
                if before != {key: calls[key] for key in identifiers}:
                    raise AssertionError("客户端断开/任务删除后仍在轮询节点")
                results.append({"round": number + 1, "subscriptions": len(identifiers), "residualTailCalls": 0})
            if (await api(client, "GET", "/api/v1/resources"))["total"] != 0:
                raise AssertionError("默认资源列表仍显示已软删除资源")
            if (await api(client, "GET", "/api/v1/tasks"))["total"] != 0:
                raise AssertionError("默认任务列表仍显示已软删除任务")
            # 历史仍存在，不能靠物理删除任务规避实时订阅回收。
            if await repo.db.tasks.count_documents({"deletedAt": {"$ne": None}}) != rounds * concurrency:
                raise AssertionError("任务软删除历史不完整")
        finally:
            stop_queries.set()
            await query
    return {"rounds": results, "resourceRequests": len(latencies),
            "resourceLatencyMs": {"p50": percentile(latencies, .5), "p99": percentile(latencies, .99),
                                  "max": round(max(latencies), 3)}}


async def main():
    """只允许本机Mongo，随机库与临时目录无条件回收，不读取生产凭据或日志。"""
    uri = Settings().mongo_uri
    if not local_mongo_uri(uri):
        raise RuntimeError("并发验收只允许回环Mongo地址")
    database = f"verify_automation_churn_{uuid4().hex}"
    mongo = AsyncMongoClient(uri, tz_aware=True)
    calls = Counter()
    node = FastAPI()

    @node.get("/internal/tail/{identifier}")
    async def tail(identifier: str):
        """故意始终无新增日志，复现发送方无法从send发现断开的场景。"""
        calls[identifier] += 1
        return {"frames": [], "gap": False}

    try:
        with TemporaryDirectory(prefix="camera-automation-churn-") as root:
            settings = Settings(mongo_uri=uri, database_name=database, start_background=False,
                                encryption_key=Fernet.generate_key().decode(), bootstrap_token=secrets.token_urlsafe(),
                                internal_token=secrets.token_urlsafe(), log_root=Path(root) / "logs", admin_password="")
            app = create_app(settings, mongo[database])
            async with serve(node) as node_port, serve(app) as api_port:
                repo = app.state.repo
                token = secrets.token_urlsafe()
                await repo.db.users.insert_one({"id": "synthetic-user", "username": "synthetic-user",
                    "displayName": "隔离并发验收", "role": "ADMIN", "isAdmin": True, "enabled": True,
                    "scopes": [], "version": 1, "authVersion": 1, "createdAt": now()})
                await repo.db.tokens.insert_one({"id": "synthetic-token", "userId": "synthetic-user",
                    "tokenHash": hashlib.sha256(token.encode()).hexdigest(), "revoked": False, "expiresAt": None})
                await repo.db.nodes.insert_one({"id": "synthetic-tail", "url": f"http://127.0.0.1:{node_port}"})
                result = await exercise(app, api_port, token, calls)
                print(json.dumps({"passed": True, "realMongo": True, "realHttpWebSocket": True,
                                  "simulatedWorkerTail": True, **result}, ensure_ascii=False))
    finally:
        try:
            await mongo.drop_database(database)
        finally:
            await mongo.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryLogDirectoryDropped": True}))


if __name__ == "__main__":
    asyncio.run(main())
