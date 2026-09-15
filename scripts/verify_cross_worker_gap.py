"""验证跨 Worker 日志目录分页与正式内容读取，不证明真实采集迁移。"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import secrets
import socket
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from camera_logs.logs.storage import HourlyWriter
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.uri_parser import parse_uri

ROOT = Path(__file__).resolve().parents[1]


def local_mongo_uri(uri):
    """随机库只允许连接全部为回环地址的 MongoDB。"""
    if uri.startswith("mongodb+srv://"):
        return False
    try:
        nodes = parse_uri(uri)["nodelist"]
        return bool(nodes) and all(host.lower() == "localhost" or ipaddress.ip_address(host).is_loopback for host, _ in nodes)
    except Exception:  # noqa: BLE001 - 不能可靠证明本机时拒绝。
        return False


def free_port():
    """为每个隔离 HTTP 进程申请一个回环端口。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def stop(process):
    """只停止本脚本创建的进程。"""
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()


def log_tail(log):
    """失败时仅保留本轮临时进程日志末尾，避免打印无界输出。"""
    log.flush()
    return Path(log.name).read_text(errors="replace")[-4000:]


async def wait_ready(url, processes):
    """实际健康轮询与进程退出检查分开，便于失败时定位。"""
    async with httpx.AsyncClient(timeout=1) as client, asyncio.timeout(45):
        while True:
            if any(process.returncode is not None for process in processes):
                raise RuntimeError("隔离 API 或 Worker 意外退出")
            try:
                if (await client.get(url)).is_success:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.2)


def environment(database, mongo_uri, root, node_id, node_port, token):
    """构造共享随机库、独立节点根目录的最小运行环境。"""
    return os.environ | {"PYTHONPATH": str(ROOT / "backend"), "MONGO_URI": mongo_uri, "DATABASE_NAME": database,
        "LOG_ROOT": str(root), "NODE_ID": node_id, "NODE_URL": f"http://127.0.0.1:{node_port}",
        "NODE_BIND_IP": "127.0.0.1", "NODE_PORT": str(node_port), "INTERNAL_TOKEN": token,
        "BOOTSTRAP_TOKEN": token, "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ADMIN_USERNAME": "cross-gap-admin", "ADMIN_PASSWORD": secrets.token_urlsafe(24), "START_BACKGROUND": "false"}


async def write_segment(root, task_id, run_id, session_id, data, stamp):
    """用正式小时写入器创建仍在写入的原始分卷，返回可由节点读取的登记字段。"""
    writer = HourlyWriter(task_id, run_id, session_id, root, storage_identity="gap-device", task_name="gap-task", device_ip="127.0.0.1")
    try:
        await writer.write(data, received_at=stamp)
        await asyncio.sleep(1.05)
        await writer.sync_due()
        snapshot = writer.snapshot()
        if not snapshot or snapshot["bytesDurable"] != len(data):
            raise RuntimeError("测试分卷未达到可读水位")
    except BaseException:
        await writer.close()
        raise
    # 不能 close：关闭会归档并删除原分卷，当前用例刻意验证节点读取活跃原始文件。
    return writer, snapshot


def segment(label, node, sequence):
    """生成超过64KiB的确定性内容，正文包含路由与顺序标记。"""
    prefix = f"node={node};sequence={sequence:06d};label={label};".encode()
    target = 70 * 1024
    repeat = (target - len(prefix) + len(label) - 1) // len(label)
    return (prefix + label.encode() * repeat)[:target]


async def read_range(client, identifier, start, end, headers):
    """按4KiB正式内容请求续读，校验每页固定nextOffset后拼接字节。"""
    chunks, offset = [], start
    while offset < end:
        limit = min(4096, end - offset)
        response = await client.get(f"/api/v1/log-files/{identifier}/content",
                                    params={"offset": offset, "limit": limit}, headers=headers)
        response.raise_for_status()
        body = response.json()
        data = base64.b64decode(body["data"])
        if not data or len(data) > limit or offset + len(data) > end:
            raise RuntimeError("正式内容接口返回了超出请求范围的字节")
        if body["nextOffset"] != offset + len(data):
            raise RuntimeError("正式内容接口未按偏移推进")
        chunks.append(data)
        offset = body["nextOffset"]
    return b"".join(chunks)


async def main():
    """启动两个真实 Worker、隔离 API，并通过正式 API 校验跨节点目录和字节。"""
    configured = Settings()
    if not local_mongo_uri(configured.mongo_uri):
        raise RuntimeError("本验证仅允许本机 MongoDB")
    database, token = f"cross_worker_gap_{uuid4().hex}", secrets.token_urlsafe(32)
    api_port, node_a_port, node_b_port = free_port(), free_port(), free_port()
    nodes = [("gap-worker-a", node_a_port), ("gap-worker-b", node_b_port)]
    processes, writers, logs = [], [], []
    worker_by_id = {}
    failed = True
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    with tempfile.TemporaryDirectory(prefix="camera-cross-worker-gap-") as temporary:
        base = Path(temporary)
        api_env = environment(database, configured.mongo_uri, base / "api", "gap-api", free_port(), token)
        api_env["NODE_URL"] = f"http://127.0.0.1:{api_port}"
        try:
            for node_id, port in nodes:
                env = environment(database, configured.mongo_uri, base / node_id, node_id, port, token)
                log = (base / f"{node_id}.log").open("w")
                logs.append(log)
                worker = await asyncio.create_subprocess_exec(sys.executable, "-m", "camera_logs.worker", cwd=ROOT, env=env, stdout=log, stderr=log)
                processes.append(worker)
                worker_by_id[node_id] = worker
            api_log = (base / "api.log").open("w")
            logs.append(api_log)
            processes.append(await asyncio.create_subprocess_exec(sys.executable, "-m", "uvicorn", "camera_logs.main:app", "--host", "127.0.0.1", "--port", str(api_port), cwd=ROOT, env=api_env, stdout=api_log, stderr=api_log))
            await wait_ready(f"http://127.0.0.1:{api_port}/health", processes)
            await asyncio.gather(*(wait_ready(f"http://127.0.0.1:{port}/health", processes) for _, port in nodes))
            stamp, task_id, run_id = datetime.now(UTC) - timedelta(minutes=2), "cross-gap-task", "cross-gap-run"
            parts = [("first", "gap-worker-a", "session-a", segment("first", "gap-worker-a", 1)),
                     ("middle", "gap-worker-b", "session-b", segment("middle", "gap-worker-b", 2)),
                     ("last", "gap-worker-a", "session-c", segment("last", "gap-worker-a", 3))]
            rows = []
            for index, (identifier, node_id, session, data) in enumerate(parts):
                root = base / node_id
                writer, snapshot = await write_segment(root, task_id, run_id, session, data, stamp + timedelta(seconds=index))
                writers.append(writer)
                rows.append({"id": identifier, "taskId": task_id, "runId": run_id, "sessionId": session, "nodeId": node_id,
                    "status": "READY", "path": snapshot["path"], "bytes": len(data), "hour": snapshot["hourStart"],
                    "segmentNumber": index + 1, "firstReceivedAt": (stamp + timedelta(seconds=index)).isoformat()})
            db = mongo[database]
            await db.tasks.insert_one({"id": task_id, "name": "跨节点缺口验证", "ip": "127.0.0.1"})
            await db.tasks.insert_one({"id": "other-task", "name": "游标隔离验证", "ip": "127.0.0.2"})
            await db.files.insert_many(rows)
            for node_id, port in nodes:
                await db.nodes.update_one({"id": node_id}, {"$set": {"id": node_id, "url": f"http://127.0.0.1:{port}"}}, upsert=True)
            headers = {"Authorization": "Bearer " + api_env["BOOTSTRAP_TOKEN"]}
            start, end = 17, len(parts[-1][3]) - 19
            params = {"beforeFileId": "first", "beforeOffset": start, "beforeSessionId": "session-a", "afterFileId": "last", "afterOffset": end, "afterSessionId": "session-c", "limit": 1}
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{api_port}", timeout=15) as client:
                catalog = []
                while True:
                    response = await client.get(f"/api/v1/tasks/{task_id}/log-gap-catalog", params=params, headers=headers)
                    response.raise_for_status()
                    page = response.json()
                    catalog.extend(page["items"])
                    if not page["nextCursor"]: break
                    params["cursor"] = page["nextCursor"]
                payload = b""
                for item in catalog:
                    payload += await read_range(client, item["fileId"], item["start"], item["end"], headers)
                expected = parts[0][3][start:] + parts[1][3] + parts[2][3][:end]
                if payload != expected or hashlib.sha256(payload).digest() != hashlib.sha256(expected).digest():
                    raise RuntimeError("跨节点分块读取的字节顺序或摘要错误")
                cursor = params.get("cursor")
                bad = await client.get("/api/v1/tasks/other-task/log-gap-catalog", params={**params, "cursor": cursor}, headers=headers)
                if bad.status_code != 422:
                    raise RuntimeError("游标未按任务隔离")
                await db.files.update_one({"id": "middle"}, {"$set": {"status": "DELETED"}})
                deleted = await client.get(f"/api/v1/tasks/{task_id}/log-gap-catalog", params={k:v for k,v in params.items() if k != "cursor"}, headers=headers)
                if "FILE_UNAVAILABLE" not in deleted.text:
                    raise RuntimeError("已删除中间文件未明确报告")
                await db.files.update_one({"id": "middle"}, {"$set": {"status": "READY"}})
                middle_ok = await client.get("/api/v1/log-files/middle/content", headers=headers)
                if middle_ok.status_code != 200:
                    raise RuntimeError("恢复后的中间节点未能读取")
                await stop(worker_by_id["gap-worker-b"])
                if worker_by_id["gap-worker-b"].returncode is None:
                    raise RuntimeError("B Worker 未正常退出")
                unavailable = await client.get("/api/v1/log-files/middle/content", headers=headers)
                if unavailable.status_code != 503:
                    raise RuntimeError("不可达节点错误返回了成功")
                first_ok = await client.get("/api/v1/log-files/first/content", headers=headers)
                if first_ok.status_code != 200:
                    raise RuntimeError("B 停止后 A 节点读取异常")
            print(json.dumps({"passed": True, "realWorkers": 2, "formalContentApi": True, "bytes": len(expected), "sha256": hashlib.sha256(expected).hexdigest(), "migrationClaim": False}, ensure_ascii=False))
            failed = False
        finally:
            writer_errors = await asyncio.gather(*(writer.close() for writer in writers), return_exceptions=True)
            stop_errors = await asyncio.gather(*(stop(process) for process in reversed(processes)), return_exceptions=True)
            if failed:
                for log in logs:
                    tail = log_tail(log)
                    if tail: print(f"--- {Path(log.name).stem} tail ---\n{tail}")
            for log in logs:
                log.close()
            try:
                await mongo.drop_database(database)
            finally:
                await mongo.close()
            if any(isinstance(error, Exception) for error in writer_errors):
                raise RuntimeError("验证写入器关闭失败")
            if any(isinstance(error, Exception) for error in stop_errors) or any(process.returncode is None for process in processes):
                raise RuntimeError("验证子进程未能确认退出，不能声明临时日志根已回收")
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryLogRootsDropped": 2}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
