"""验证真实 Worker 收到 SIGTERM 后关闭连接，并由新 Worker 自动恢复 SSH 采集。

脚本仅使用回环 AsyncSSH、回环 ISAPI、随机本机 MongoDB 和临时日志根。任务的资源、
创建、停止及自动重新领取均走正式 API/调度器；MongoDB 只用于只读核查端点名额和文件。
默认 ISAPI 使用端口80。非特权本机可传 ``--isapi-test-port``，仅隔离 API 子进程适配端口。
"""

import argparse
import asyncio
import json
import secrets
import signal
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from cross_worker_ssh_resume_source import INITIAL_COMMANDS, DeviceInfoSource, SshSource
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from verify_cross_worker_ssh_resume import (
    ROOT,
    environment,
    free_port,
    local_mongo_uri,
    operation,
    stop,
    verify_session_log,
    wait_ready,
    wait_task,
)


def process_environment(database, mongo_uri, root, node_id, port, token, key, password):
    """复用共享密钥的最小 Worker 环境；API 的后台调度单独显式开启。"""
    return environment(database, mongo_uri, root, node_id, port, token, key, password)


async def start_worker(database, mongo_uri, root, node_id, port, token, key, password, log):
    """以稳定节点身份启动真实 Worker，替代容器重启后的同一节点进程。"""
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "camera_logs.worker",
        cwd=ROOT,
        env=process_environment(database, mongo_uri, root, node_id, port, token, key, password),
        stdout=log,
        stderr=log,
    )


async def current_slots(database):
    """只读确认所有 SSH 名额已释放；不能以任务展示状态代替真实占位。"""
    return await database.ssh_connection_slots.count_documents({"claims": {"$ne": []}})


def awaiting_replacement(task):
    """判断任务已无节点且保留运行意图，供SIGTERM后的状态观察使用。"""
    # 正常调度可在观察前将STOPPED转为PENDING；两者都必须没有节点归属。
    return (task.get("status") in {"STOPPED", "PENDING"}
            and task.get("desiredState") == "RUNNING" and task.get("nodeId") is None)


async def main(isapi_test_port=80):
    """启动采集、向 Worker 发送 SIGTERM、替换进程并验证新运行。"""
    configured = Settings()
    if not local_mongo_uri(configured.mongo_uri):
        raise RuntimeError("本验证仅允许本机 MongoDB")
    database_name = f"sigterm_resume_{uuid4().hex}"
    token, key, password = (
        secrets.token_urlsafe(32),
        Fernet.generate_key().decode(),
        secrets.token_urlsafe(24),
    )
    api_port, worker_port = free_port(), free_port()
    node_id, task_id, failed = "sigterm-worker", None, True
    source, device = SshSource(password), DeviceInfoSource(password, isapi_test_port)
    processes, logs = [], []
    database = AsyncMongoClient(
        configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True
    )[database_name]
    with tempfile.TemporaryDirectory(prefix="camera-worker-sigterm-") as temporary:
        root = Path(temporary)
        try:
            await device.start()
            await source.start()
            api_env = environment(
                database_name,
                configured.mongo_uri,
                root / "api",
                "sigterm-api",
                api_port,
                token,
                key,
                password,
                start_background=True,
            )
            if isapi_test_port != 80:
                api_env["ISAPI_TEST_PORT"] = str(isapi_test_port)
            worker_log = (root / "worker-first.log").open("w")
            logs.append(worker_log)
            first_worker = await start_worker(
                database_name,
                configured.mongo_uri,
                root / node_id,
                node_id,
                worker_port,
                token,
                key,
                password,
                worker_log,
            )
            processes.append(first_worker)
            api_log = (root / "api.log").open("w")
            logs.append(api_log)
            api = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "scripts.cross_worker_ssh_resume_api",
                cwd=ROOT,
                env=api_env,
                stdout=api_log,
                stderr=api_log,
            )
            processes.append(api)
            await wait_ready(f"http://127.0.0.1:{api_port}/health", processes)
            await wait_ready(f"http://127.0.0.1:{worker_port}/health", processes)
            headers = {"Authorization": "Bearer " + token}
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{api_port}", headers=headers, timeout=20
            ) as client:
                registered = await client.post(
                    "/api/v1/admin/nodes",
                    json={
                        "id": node_id,
                        "url": f"http://127.0.0.1:{worker_port}",
                        "capacity": 16,
                        "accepting": True,
                    },
                )
                registered.raise_for_status()
                resource = await client.post(
                    "/api/v1/resources",
                    headers={"Idempotency-Key": "sigterm-resource-" + uuid4().hex},
                    json={
                        "name": "SIGTERM回环设备",
                        "kind": "HIKVISION_NETWORK",
                        "ip": "127.0.0.1",
                        "username": "collector",
                        "password": password,
                        "authType": "BASIC",
                    },
                )
                resource.raise_for_status()
                task = await client.post(
                    "/api/v1/tasks",
                    headers={"Idempotency-Key": "sigterm-task-" + uuid4().hex},
                    json={
                        "name": "SIGTERM自动恢复",
                        "description": "",
                        "sharedWith": [],
                        "sharedWithAll": False,
                        "protocol": "SSH",
                        "ip": "127.0.0.1",
                        "port": source.port,
                        "username": "collector",
                        "password": password,
                        "resourceId": resource.json()["id"],
                        "autoStart": True,
                        "initialCommands": [{"command": item} for item in INITIAL_COMMANDS],
                        "scheduledCommands": [],
                    },
                )
                task.raise_for_status()
                task_id = task.json()["id"]
                first = await wait_task(
                    client,
                    task_id,
                    lambda item: item.get("status") == "COLLECTING" and item.get("nodeId") == node_id,
                    "首个 Worker 未开始采集",
                )
                await source.wait_commands(1, expect_probe=False)
                first_lines = await source.suspend_after(1)
                first_log = await verify_session_log(
                    client, database, task_id, node_id, 1, first_lines, session_id=first["sessionId"]
                )
                first_worker.send_signal(signal.SIGTERM)
                await asyncio.wait_for(first_worker.wait(), 30)
                released = await wait_task(
                    client,
                    task_id,
                    awaiting_replacement,
                    "SIGTERM 后任务未完成可恢复收尾",
                )
                if source.active or await current_slots(database):
                    raise RuntimeError("SIGTERM 后 SSH 连接或名额未释放")
                old_run = await database.runs.find_one({"id": first["runId"]})
                if released.get("runId") != first["runId"] or not old_run or not old_run.get("endedAt"):
                    raise RuntimeError("SIGTERM 后旧运行未确认结束")
                if await database.endpoint_locks.count_documents({"taskId": task_id, "runId": first["runId"]}):
                    raise RuntimeError("SIGTERM 后旧运行端点锁未释放")
                replacement_log = (root / "worker-replacement.log").open("w")
                logs.append(replacement_log)
                replacement = await start_worker(
                    database_name,
                    configured.mongo_uri,
                    root / node_id,
                    node_id,
                    worker_port,
                    token,
                    key,
                    password,
                    replacement_log,
                )
                processes.append(replacement)
                await wait_ready(f"http://127.0.0.1:{worker_port}/health", [api, replacement])
                second = await wait_task(
                    client,
                    task_id,
                    lambda item: (
                        item.get("status") == "COLLECTING"
                        and item.get("nodeId") == node_id
                        and item.get("runId") != first.get("runId")
                        and item.get("sessionId") != first.get("sessionId")
                    ),
                    "替换 Worker 未创建新的采集运行",
                )
                await source.wait_commands(2, expect_probe=False)
                source.resume_emitting()
                second_lines = await source.suspend_after(2)
                second_log = await verify_session_log(
                    client, database, task_id, node_id, 2, second_lines, session_id=second["sessionId"]
                )
                stopped = await client.post(f"/api/v1/tasks/{task_id}/stop")
                stopped.raise_for_status()
                await operation(client, stopped.json()["id"])
                await asyncio.wait_for(source.closed.wait(), 15)
                final = await wait_task(
                    client,
                    task_id,
                    lambda item: (
                        item.get("status") == "STOPPED"
                        and item.get("desiredState") == "STOPPED"
                        and item.get("nodeId") is None
                    ),
                    "验证收尾停止失败",
                )
                if source.active or await current_slots(database):
                    raise RuntimeError("最终停止后 SSH 连接或名额残留")
                print(
                    json.dumps(
                        {
                            "passed": True,
                            "signal": "SIGTERM",
                            "sameNode": second["nodeId"] == node_id,
                            "newRun": first["runId"] != second["runId"],
                            "sessions": source.connection_count,
                            "initialCommands": source.command_lines(),
                            "actualLogs": [first_log, second_log],
                            "releaseBeforeReplacement": awaiting_replacement(released),
                            "releaseObservedStatus": released["status"],
                            "finalStatus": final["status"],
                            "isapiTestPort": isapi_test_port,
                        },
                        ensure_ascii=False,
                    )
                )
                failed = False
        finally:
            stopped = await asyncio.gather(
                *(stop(process) for process in reversed(processes)), return_exceptions=True
            )
            source_result = await asyncio.gather(source.close(), device.close(), return_exceptions=True)
            errors = [item for item in [*stopped, *source_result] if isinstance(item, BaseException)]
            for log in logs:
                try:
                    if failed:
                        log.flush()
                        tail = Path(log.name).read_text(errors="replace")[-4000:]
                        if tail:
                            print(f"{Path(log.name).stem} tail:\n{tail}")
                except Exception as exc:  # noqa: BLE001 - 诊断失败也必须继续回收随机数据库。
                    errors.append(exc)
                finally:
                    try:
                        log.close()
                    except Exception as exc:  # noqa: BLE001 - 汇总关闭错误后继续其它清理步骤。
                        errors.append(exc)
            client = database.client
            try:
                await client.drop_database(database_name)
            finally:
                await client.close()
            if errors or any(process.returncode is None for process in processes):
                raise RuntimeError("隔离进程或回环源未能确认收尾，不能声明临时日志根已回收")
    print(
        json.dumps(
            {"temporaryDatabaseDropped": True, "temporaryLogRootsDropped": 1, "failed": failed},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--isapi-test-port", type=int, default=80)
    arguments = parser.parse_args()
    if not 1 <= arguments.isapi_test_port <= 65535:
        raise ValueError("--isapi-test-port 必须在1至65535")
    asyncio.run(main(arguments.isapi_test_port))
