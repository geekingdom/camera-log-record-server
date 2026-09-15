"""验证真实 SSH 采集经暂停后在另一 Worker 继续同一运行。

仅使用回环 ISAPI/AsyncSSH 源、随机本机 Mongo 数据库和临时日志目录；不访问实体设备。
脚本经正式 API 创建资源和任务、切换节点准入并读取实际采集文件，不能证明物理跨机接管。
"""

import argparse
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
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from cross_worker_ssh_resume_source import INITIAL_COMMANDS, DeviceInfoSource, SshSource
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.uri_parser import parse_uri

ROOT = Path(__file__).resolve().parents[1]


def local_mongo_uri(uri):
    """随机数据库只允许连接所有成员均为回环地址的 Mongo。"""
    if uri.startswith("mongodb+srv://"):
        return False
    try:
        nodes = parse_uri(uri)["nodelist"]
        return bool(nodes) and all(
            host == "localhost" or ipaddress.ip_address(host).is_loopback
            for host, _ in nodes
        )
    except Exception:  # noqa: BLE001 - 无法证明本机时拒绝执行。
        return False


def free_port():
    """分配一个短暂占用的本机端口给隔离子进程。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def environment(database, uri, root, node_id, port, token, key, password, *, start_background=False):
    """全部子进程共享随机密钥、数据库与内部令牌，节点日志根独立。"""
    return os.environ | {
        "PYTHONPATH": str(ROOT / "backend"),
        "MONGO_URI": uri,
        "DATABASE_NAME": database,
        "LOG_ROOT": str(root),
        "NODE_ID": node_id,
        "NODE_URL": f"http://127.0.0.1:{port}",
        "NODE_BIND_IP": "127.0.0.1",
        "NODE_PORT": str(port),
        "INTERNAL_TOKEN": token,
        "BOOTSTRAP_TOKEN": token,
        "ENCRYPTION_KEY": key,
        "ADMIN_USERNAME": "resume-admin",
        "ADMIN_PASSWORD": password,
        "START_BACKGROUND": str(start_background).lower(),
    }


async def stop(process):
    """只结束本脚本创建的子进程，并确认退出。"""
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except TimeoutError:
            process.kill()
            await process.wait()


async def wait_ready(url, processes):
    """等待正式 HTTP 健康端点，进程早退立即失败。"""
    async with httpx.AsyncClient(timeout=1) as client, asyncio.timeout(45):
        while True:
            if any(item.returncode is not None for item in processes):
                raise RuntimeError("隔离子进程提前退出")
            try:
                if (await client.get(url)).is_success:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.2)


async def operation(client, identifier):
    """等待异步控制操作落定，拒绝以短暂状态代替结果。"""
    async with asyncio.timeout(45):
        while True:
            value = (await client.get(f"/api/v1/operations/{identifier}")).json()
            if value["status"] == "SUCCEEDED":
                return value
            if value["status"] != "PENDING":
                raise RuntimeError("控制操作失败: " + value["status"])
            await asyncio.sleep(0.2)


async def wait_task(client, task_id, predicate, message):
    """轮询任务直到指定状态合同成立。"""
    async with asyncio.timeout(45):
        while True:
            task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
            if predicate(task):
                return task
            if task.get("status") in {"ERROR", "BLOCKED"}:
                raise RuntimeError(f"{message}: {task}")
            await asyncio.sleep(0.2)


async def read_file(client, identifier):
    """经正式内容接口续读单个真实采集文件，拒绝用数据库路径绕过节点读取。"""
    chunks, offset = [], 0
    while True:
        response = await client.get(
            f"/api/v1/log-files/{identifier}/content", params={"offset": offset, "limit": 65536}
        )
        response.raise_for_status()
        page = response.json()
        data = base64.b64decode(page["data"])
        if page["nextOffset"] != offset + len(data):
            raise RuntimeError("正式日志内容接口偏移未连续推进")
        chunks.append(data)
        if not data:
            return b"".join(chunks)
        offset = page["nextOffset"]


async def verify_session_log(client, database, task_id, node_id, session, expected):
    """等真实文件水位包含有限源样本，再按原始正文逐条验证连续且没有重复。"""
    expected = [line.rstrip(b"\n") for line in expected]
    async with asyncio.timeout(8):
        while True:
            files = [item async for item in database.files.find(
                {"taskId": task_id, "nodeId": node_id}
            ).sort([("sessionStartedAt", 1), ("firstSequence", 1), ("id", 1)])]
            content = b"".join(await asyncio.gather(*(read_file(client, item["id"]) for item in files)))
            actual = []
            for line in content.splitlines():
                if not line.startswith(b"[") or b"] " not in line:
                    raise RuntimeError("正式采集日志缺少服务器行首前缀")
                actual.append(line.split(b"] ", 1)[1])
            if actual == expected:
                return {"nodeId": node_id, "session": session, "lines": len(actual), "fileCount": len(files),
                        "bodySha256": hashlib.sha256(b"\n".join(actual) + b"\n").hexdigest()}
            if len(actual) > len(expected) or (actual and actual != expected[: len(actual)]):
                raise RuntimeError(f"节点 {node_id} 会话 {session} 日志乱序或重复")
            await asyncio.sleep(0.2)


async def main(isapi_test_port=80):
    """执行 A 采集、暂停、正式准入切换、B 恢复和文件/锁收尾验证。"""
    configured = Settings()
    if not local_mongo_uri(configured.mongo_uri):
        raise RuntimeError("本验证仅允许本机 MongoDB")
    database, token, key, password = (
        f"cross_ssh_resume_{uuid4().hex}",
        secrets.token_urlsafe(32),
        Fernet.generate_key().decode(),
        secrets.token_urlsafe(24),
    )
    api_port, a_port, b_port = free_port(), free_port(), free_port()
    processes, logs, task_id = [], [], None
    source, device = SshSource(password), DeviceInfoSource(password, isapi_test_port)
    mongo = AsyncMongoClient(
        configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True
    )
    failed = True
    with tempfile.TemporaryDirectory(prefix="camera-cross-ssh-resume-") as temporary:
        base = Path(temporary)
        try:
            await device.start()
            await source.start()
            api_env = environment(
                database,
                configured.mongo_uri,
                base / "api",
                "resume-api",
                api_port,
                token,
                key,
                password,
                start_background=True,
            )
            if isapi_test_port != 80:
                api_env["ISAPI_TEST_PORT"] = str(isapi_test_port)
            for node_id, port in (("resume-a", a_port), ("resume-b", b_port)):
                log = (base / f"{node_id}.log").open("w")
                logs.append(log)
                processes.append(
                    await asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "camera_logs.worker",
                        cwd=ROOT,
                        env=environment(
                            database,
                            configured.mongo_uri,
                            base / node_id,
                            node_id,
                            port,
                            token,
                            key,
                            password,
                        ),
                        stdout=log,
                        stderr=log,
                    )
                )
            api_log = (base / "api.log").open("w")
            logs.append(api_log)
            processes.append(
                await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "scripts.cross_worker_ssh_resume_api",
                    cwd=ROOT,
                    env=api_env,
                    stdout=api_log,
                    stderr=api_log,
                )
            )
            await wait_ready(f"http://127.0.0.1:{api_port}/health", processes)
            await asyncio.gather(
                *(wait_ready(f"http://127.0.0.1:{port}/health", processes) for port in (a_port, b_port))
            )
            headers = {"Authorization": "Bearer " + token}
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{api_port}", headers=headers, timeout=15
            ) as client:
                for node_id, port, accepting in (("resume-a", a_port, True), ("resume-b", b_port, False)):
                    response = await client.post(
                        "/api/v1/admin/nodes",
                        json={
                            "id": node_id,
                            "url": f"http://127.0.0.1:{port}",
                            "capacity": 10,
                            "accepting": accepting,
                        },
                    )
                    response.raise_for_status()
                resource = await client.post(
                    "/api/v1/resources",
                    headers={"Idempotency-Key": "resource-" + uuid4().hex},
                    json={
                        "name": "本机恢复源",
                        "kind": "HIKVISION_NETWORK",
                        "ip": "127.0.0.1",
                        "username": "collector",
                        "password": password,
                        "authType": "BASIC",
                    },
                )
                resource.raise_for_status()
                resource_id = resource.json()["id"]
                task = await client.post(
                    "/api/v1/tasks",
                    headers={"Idempotency-Key": "task-" + uuid4().hex},
                    json={
                        "name": "跨节点SSH恢复",
                        "description": "",
                        "sharedWith": [],
                        "sharedWithAll": False,
                        "protocol": "SSH",
                        "ip": "127.0.0.1",
                        "port": source.port,
                        "username": "collector",
                        "password": password,
                        "resourceId": resource_id,
                        "autoStart": True,
                        "initialCommands": [{"command": item} for item in INITIAL_COMMANDS],
                        "scheduledCommands": [
                            {"command": "probe", "totalExecutions": 2, "intervalSeconds": 5}
                        ],
                    },
                )
                if not task.is_success:
                    raise RuntimeError("正式创建 SSH 任务失败: " + task.text[:2000])
                task_id = task.json()["id"]
                first = await wait_task(
                    client,
                    task_id,
                    lambda x: x.get("status") == "COLLECTING" and x.get("nodeId") == "resume-a",
                    "A 未开始采集",
                )
                await source.wait_commands(1)
                first_lines = await source.suspend_after(1)
                first_log = await verify_session_log(
                    client, mongo[database], task_id, "resume-a", 1, first_lines
                )
                paused = await client.post(f"/api/v1/tasks/{task_id}/pause")
                paused.raise_for_status()
                await operation(client, paused.json()["id"])
                await asyncio.wait_for(source.closed.wait(), 15)
                await wait_task(
                    client,
                    task_id,
                    lambda x: x.get("status") == "PAUSED" and x.get("nodeId") is None,
                    "暂停未完成",
                )
                claims = await mongo[database].ssh_connection_slots.count_documents({"claims": {"$ne": []}})
                if claims or source.active or source.connection_count != 1:
                    raise RuntimeError("暂停后 SSH 连接或名额未释放")
                await asyncio.sleep(2.2)
                if source.active or await mongo[database].ssh_connection_slots.count_documents(
                    {"claims": {"$ne": []}}
                ):
                    raise RuntimeError("暂停窗口发生自动重连或 SSH 名额回流")
                nodes = (await client.get("/api/v1/admin/nodes")).json()["items"]
                for node in nodes:
                    accepting = node["id"] == "resume-b"
                    changed = await client.patch(
                        f"/api/v1/admin/nodes/{node['id']}",
                        json={"version": node["version"], "accepting": accepting},
                    )
                    changed.raise_for_status()
                # resume 必须等待一次新 ISAPI 成功响应；在响应放行前重复请求只能复用同一操作。
                device.requests_seen.clear()
                device.allow_requests.clear()
                resumed = await client.post(f"/api/v1/tasks/{task_id}/resume")
                resumed.raise_for_status()
                repeated = await client.post(f"/api/v1/tasks/{task_id}/resume")
                repeated.raise_for_status()
                if repeated.json()["id"] != resumed.json()["id"]:
                    raise RuntimeError("重复 resume 未复用原操作")
                if not await asyncio.to_thread(device.requests_seen.wait, 20):
                    raise TimeoutError("resume 后未触发新的 ISAPI 健康复验")
                waiting = await wait_task(
                    client,
                    task_id,
                    lambda x: x.get("status") == "WAITING_DEVICE" and x.get("nodeId") is None,
                    "ISAPI 闸门期间未进入等待设备",
                )
                if waiting.get("runId") != first.get("runId") or waiting.get("desiredState") != "RUNNING":
                    raise RuntimeError("等待设备时丢失原运行或恢复意图")
                if (await client.get(f"/api/v1/operations/{resumed.json()['id']}")).json()[
                    "status"
                ] != "PENDING":
                    raise RuntimeError("健康复验未完成时 resume 操作不应完成")
                if source.active or await mongo[database].ssh_connection_slots.count_documents(
                    {"claims": {"$ne": []}}
                ):
                    raise RuntimeError("健康复验闸门期间意外建立 SSH 连接或名额")
                await asyncio.sleep(2.2)
                if source.active or await mongo[database].ssh_connection_slots.count_documents(
                    {"claims": {"$ne": []}}
                ):
                    raise RuntimeError("等待设备期间发生自动重连")
                device.allow_requests.set()
                source.resume_emitting()
                second = await wait_task(
                    client,
                    task_id,
                    lambda x: (
                        x.get("status") == "COLLECTING"
                        and x.get("nodeId") == "resume-b"
                        and x.get("runId") == first.get("runId")
                        and x.get("sessionId") != first.get("sessionId")
                    ),
                    "B 未以同一运行恢复",
                )
                resource_after = (await client.get(f"/api/v1/resources/{resource_id}")).json()
                requested_at = datetime.fromisoformat(waiting["resumeWaiting"]["requestedAt"]).astimezone(UTC)
                health_checked = resource_after.get("healthCheckedAt")
                if (
                    not health_checked
                    or datetime.fromisoformat(health_checked).astimezone(UTC) <= requested_at
                ):
                    raise RuntimeError("恢复后资源认证时间未晚于等待请求")
                await source.wait_commands(2)
                await operation(client, resumed.json()["id"])
                executions = (
                    await client.get(
                        f"/api/v1/tasks/{task_id}/command-executions", params={"kind": "SCHEDULED"}
                    )
                ).json()["items"]
                if len([item for item in executions if item.get("status") == "SENT"]) != 2:
                    raise RuntimeError("定时 probe 预算未保持累计2次")
                second_lines = await source.suspend_after(2)
                second_log = await verify_session_log(
                    client, mongo[database], task_id, "resume-b", 2, second_lines
                )
                stopped = await client.post(f"/api/v1/tasks/{task_id}/stop")
                stopped.raise_for_status()
                await operation(client, stopped.json()["id"])
                await asyncio.wait_for(source.closed.wait(), 15)
                final = await wait_task(
                    client,
                    task_id,
                    lambda x: x.get("status") == "STOPPED" and x.get("nodeId") is None,
                    "停止未释放任务",
                )
                if (
                    await mongo[database].ssh_connection_slots.count_documents({"claims": {"$ne": []}})
                    or source.active
                ):
                    raise RuntimeError("停止后 SSH 锁或连接残留")
                received_commands = source.command_lines()
                if [items.count("probe") for items in received_commands] != [1, 1]:
                    raise RuntimeError(f"定时 probe 未按 A 一次/B 一次执行: {received_commands!r}")
                print(
                    json.dumps(
                        {
                            "passed": True,
                            "sameRun": first["runId"] == second["runId"],
                            "sessions": source.connection_count,
                            "initialCommands": received_commands,
                            "scheduledSent": 2,
                            "resumeHealthRevalidated": True,
                            "duplicateResumeReusedOperation": True,
                            "pauseReleasedSlots": True,
                            "noPauseReconnectSeconds": 2.2,
                            "actualLogs": [first_log, second_log],
                            "isapiTestPort": isapi_test_port,
                            "finalStatus": final["status"],
                        },
                        ensure_ascii=False,
                    )
                )
                failed = False
        finally:
            cleanup_errors = []
            stopped = await asyncio.gather(
                *(stop(item) for item in reversed(processes)), return_exceptions=True
            )
            cleanup_errors.extend(item for item in stopped if isinstance(item, BaseException))
            for source_cleanup in (source.close(), device.close()):
                result = await asyncio.gather(source_cleanup, return_exceptions=True)
                cleanup_errors.extend(item for item in result if isinstance(item, BaseException))
            for log in logs:
                try:
                    if failed:
                        log.flush()
                        tail = Path(log.name).read_text(errors="replace")[-4000:]
                        if tail:
                            print(f"{Path(log.name).stem} tail:\n{tail}")
                except Exception as exc:  # noqa: BLE001 - 汇总后报错，诊断失败不能跳过数据库清理。
                    cleanup_errors.append(exc)
                finally:
                    try:
                        log.close()
                    except Exception as exc:  # noqa: BLE001 - 继续关闭其它句柄并清理随机数据库。
                        cleanup_errors.append(exc)
            try:
                await mongo.drop_database(database)
            finally:
                await mongo.close()
            if cleanup_errors or any(item.returncode is None for item in processes):
                raise RuntimeError("隔离进程或回环源未能确认收尾，不能声明临时日志根已回收")
    print(
        json.dumps(
            {"temporaryDatabaseDropped": True, "temporaryLogRootsDropped": 2, "failed": failed},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--isapi-test-port", type=int, default=80, help="非特权本机的隔离 ISAPI 端口；CI 默认80"
    )
    arguments = parser.parse_args()
    if not 1 <= arguments.isapi_test_port <= 65535:
        raise ValueError("--isapi-test-port 必须在1至65535")
    asyncio.run(main(arguments.isapi_test_port))
