"""用正式 API 验证一条显式 SSH 任务在设备重启后的离线和自动重连恢复。

必须传入 --reboot-confirm 才会提交 reboot 命令。本工具不建立额外设备 SSH
连接、不读取日志正文，并在 finally 中经正式 stop API 回收采集连接。共享端点
模式只解释其它可见活动任务的 socket，不会控制它们。
"""

import argparse
import asyncio
import json
import time
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from verify_ssh_lifecycle import (
    ACTIVE_STATUSES,
    connection_counts,
    validate_task,
    wait_operation,
)


def public_task_state(task):
    """报告运行身份与状态，不输出设备配置、命令正文或凭据。"""
    return {key: task.get(key) for key in ("id", "status", "desiredState", "runId", "sessionId", "nodeId")}


def public_resource_state(resource):
    """仅记录健康状态及检查时间，避免报告资源凭据或设备返回内容。"""
    return {key: resource.get(key) for key in ("id", "healthStatus", "healthCheckedAt", "authenticatedAt")}


async def get_task(client, task_id):
    """从正式 API 读取任务当前状态。"""
    response = await client.get(f"/api/v1/tasks/{task_id}")
    response.raise_for_status()
    return response.json()


async def get_resource(client, resource_id):
    """从正式 API 读取任务绑定设备的健康状态。"""
    response = await client.get(f"/api/v1/resources/{resource_id}")
    response.raise_for_status()
    return response.json()


async def list_tasks(client, params):
    """分页读取管理员可见任务，用于端点连接解释和恢复意图预检。"""
    page, tasks = 1, []
    while True:
        response = await client.get("/api/v1/tasks", params=params | {"page": page, "pageSize": 100})
        response.raise_for_status()
        result = response.json()
        tasks.extend(result["items"])
        if len(tasks) >= result["total"] or not result["items"]:
            return tasks
        page += 1


async def load_reboot_task(client, task_id, allow_shared_endpoint):
    """读取唯一测试任务和同资源/端点任务；默认拒绝任何可能自动恢复的竞争者。"""
    task = await get_task(client, task_id)
    validate_task(task)
    resource_id = task.get("resourceId")
    if not resource_id:
        raise ValueError("任务缺少绑定资源，无法验证健康状态")
    resource_tasks, endpoint_tasks = await asyncio.gather(
        list_tasks(client, {"resourceId": resource_id}),
        list_tasks(client, {"search": task["ip"]}),
    )
    endpoint_tasks = [item for item in endpoint_tasks if item.get("ip") == task["ip"] and item.get("port") == task["port"]]
    if task_id not in {item.get("id") for item in endpoint_tasks}:
        endpoint_tasks.append(task)
    potential = [item for item in resource_tasks if item.get("resourceHealthRecovery") or item.get("restartRequested")]
    active = [item for item in endpoint_tasks if item.get("id") != task_id and _endpoint_active(item)]
    if not allow_shared_endpoint and potential:
        raise ValueError("同资源存在待恢复或受控重启任务: " + ", ".join(item["id"] for item in potential))
    if not allow_shared_endpoint and active:
        raise ValueError("任务端点已有其他活动任务: " + ", ".join(item["id"] for item in active))
    return task, endpoint_tasks, potential


def _endpoint_active(task):
    """仅把可能持有或即将建立 socket 的运行状态计入端点总连接上限。"""
    return task.get("status") in ACTIVE_STATUSES or task.get("nodeId") is not None


async def check_endpoint_sockets(client, worker_pid, task, allow_shared_endpoint):
    """按可见同端点活动任务数解释 socket 总量；两次快照仍超限才判定重叠异常。"""
    if not allow_shared_endpoint:
        total = connection_counts(worker_pid, [task])[task["id"]]
        if total > 1:
            raise RuntimeError("检测到测试任务端点存在多个 SSH 连接")
        return {"socketTotal": total, "allowedActiveTasks": 1, "activeTaskIds": [task["id"]]}
    for _attempt in range(2):
        endpoint_tasks = await list_tasks(client, {"search": task["ip"]})
        active = [item for item in endpoint_tasks if item.get("ip") == task["ip"] and item.get("port") == task["port"]
                  and _endpoint_active(item)]
        total = connection_counts(worker_pid, [task])[task["id"]]
        detail = {"socketTotal": total, "allowedActiveTasks": len(active), "activeTaskIds": [item["id"] for item in active]}
        if total <= len(active):
            return detail
        await asyncio.sleep(.1)
    raise RuntimeError("端点 SSH 总连接数无法由可见活动任务解释: " + json.dumps(detail, ensure_ascii=False))


async def start_and_wait_collecting(client, task_id, worker_pid, allow_shared_endpoint=False):
    """通过控制 API 启动任务，并在等待操作时限制该任务只有一个 SSH socket。"""
    response = await client.post(f"/api/v1/tasks/{task_id}/start")
    response.raise_for_status()
    operation_id, deadline, operation_succeeded = response.json()["id"], time.monotonic() + 120, False
    while time.monotonic() < deadline:
        task = await get_task(client, task_id)
        await check_endpoint_sockets(client, worker_pid, task, allow_shared_endpoint)
        if operation_succeeded:
            if task.get("status") != "COLLECTING" or task.get("desiredState") != "RUNNING":
                if task.get("status") in {"ERROR", "BLOCKED"}:
                    raise RuntimeError("启动操作完成后任务进入失败状态: " + json.dumps(public_task_state(task), ensure_ascii=False))
            else:
                return task
        else:
            operation = await client.get(f"/api/v1/operations/{operation_id}")
            operation.raise_for_status()
            status = operation.json()["status"]
            if status == "SUCCEEDED":
                # 操作成功与任务状态回写是两次请求；下一轮重新读取最新任务快照。
                operation_succeeded = True
            elif status != "PENDING":
                raise RuntimeError("启动操作失败: " + status)
        await asyncio.sleep(.5)
    raise TimeoutError("启动操作完成后等待任务进入 COLLECTING 超时" if operation_succeeded else "等待启动操作完成超时")


async def wait_command_sent(client, command_id, timeout=30):
    """等待 reboot 已从唯一发送队列写入当前采集会话。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/commands/{command_id}")
        response.raise_for_status()
        command = response.json()
        if command.get("status") in {"SENT", "UNKNOWN"}:
            return command
        if command.get("status") in {"FAILED", "CANCELLED"}:
            raise RuntimeError("reboot 命令未进入设备发送路径: " + command["status"])
        await asyncio.sleep(.5)
    raise TimeoutError("等待 reboot 命令发送超时")


async def wait_reboot_recovery(client, task_id, resource_id, worker_pid, transitions, allow_shared_endpoint, timeout=300):
    """观察系统离线停止后的新运行恢复至 COLLECTING，记录每次公开状态变化。"""
    started_at = time.monotonic()
    deadline, offline_at = started_at + timeout, None
    last = None
    while time.monotonic() < deadline:
        task, resource = await get_task(client, task_id), await get_resource(client, resource_id)
        socket_detail = await check_endpoint_sockets(client, worker_pid, task, allow_shared_endpoint)
        current = (task.get("status"), task.get("runId"), task.get("sessionId"), resource.get("healthStatus"))
        if current != last:
            transitions.append({"atSeconds": round(time.monotonic() - started_at, 3), "task": public_task_state(task),
                                "resource": public_resource_state(resource), "endpointSockets": socket_detail})
            last = current
        if resource.get("healthStatus") == "OFFLINE":
            offline_at = offline_at or time.monotonic()
        if offline_at is not None and task.get("status") == "COLLECTING" and resource.get("healthStatus") == "ONLINE":
            return task, resource, transitions, round(time.monotonic() - offline_at, 3)
        await asyncio.sleep(.5)
    raise TimeoutError("300 秒内未观察到设备 OFFLINE 后重新 COLLECTING")


async def cleanup_test_task(client, task_id, worker_pid, allow_shared_endpoint):
    """只停止测试任务；其它端点连接必须由可见活动任务解释，绝不控制其任务。"""
    try:
        response = await client.post(f"/api/v1/tasks/{task_id}/stop")
        response.raise_for_status()
        await wait_operation(client, response.json()["id"])
        task = await get_task(client, task_id)
        if task.get("status") != "STOPPED" or task.get("nodeId") is not None:
            raise RuntimeError("测试任务停止后仍未完成节点回收")
        return "SUCCEEDED", await check_endpoint_sockets(client, worker_pid, task, allow_shared_endpoint)
    except Exception as error:  # noqa: BLE001 - 报告收尾错误而不停止其它用户任务。
        return type(error).__name__, None


async def execute(args):
    """启动指定停止态任务，发送重启命令并始终停止回收。"""
    if not args.reboot_confirm:
        raise ValueError("必须显式传入 --reboot-confirm 才会向设备发送 reboot")
    report = {"passed": False, "taskId": args.task_id, "timeoutSeconds": args.timeout,
              "allowSharedEndpoint": args.allow_shared_endpoint, "transitions": [], "cleanup": {}}
    if not 1 <= args.timeout <= 300:
        raise ValueError("--timeout 必须在 1 至 300 秒之间")
    workflow_error, transitions = None, []
    async with httpx.AsyncClient(base_url=args.url, headers={"Authorization": "Bearer " + Settings().bootstrap_token}, timeout=30) as client:
        task, endpoint_tasks, potential = await load_reboot_task(client, args.task_id, args.allow_shared_endpoint)
        resource_id = task.get("resourceId")
        report["preflight"] = {"sameEndpointTaskIds": [item["id"] for item in endpoint_tasks],
                               "pendingRecoveryOrRestartTaskIds": [item["id"] for item in potential]}
        try:
            initial_resource = await get_resource(client, resource_id)
            if initial_resource.get("healthStatus") != "ONLINE":
                raise RuntimeError("设备资源必须先处于 ONLINE")
            report["initialResource"] = public_resource_state(initial_resource)
            started = await start_and_wait_collecting(client, task["id"], args.worker_pid, args.allow_shared_endpoint)
            report["started"] = public_task_state(started)
            report["transitions"] = transitions
            response = await client.post(
                f"/api/v1/tasks/{task['id']}/commands", json={"command": "reboot"},
                headers={"Idempotency-Key": "reboot-recovery-" + uuid4().hex},
            )
            response.raise_for_status()
            command = await wait_command_sent(client, response.json()["id"])
            report["command"] = {"id": command.get("id"), "status": command.get("status")}
            recovered, resource, transitions, offline_seconds = await wait_reboot_recovery(
                client, task["id"], resource_id, args.worker_pid, transitions, args.allow_shared_endpoint, args.timeout)
            if recovered.get("runId") == started.get("runId") or recovered.get("sessionId") == started.get("sessionId"):
                raise RuntimeError("设备重启后运行或会话身份不符合重连契约")
            report.update(recovered=public_task_state(recovered), resource=public_resource_state(resource),
                          offlineToRecoverySeconds=offline_seconds, transitions=transitions, passed=True)
        except Exception as error:  # noqa: BLE001 - 实机验证失败仍必须经正式 API 停止。
            workflow_error = error
            report["error"] = {"type": type(error).__name__, "message": str(error)}
        finally:
            cleanup_status, cleanup_sockets = await cleanup_test_task(
                client, args.task_id, args.worker_pid, args.allow_shared_endpoint)
            report["cleanup"] = {args.task_id: cleanup_status}
            report["cleanupEndpointSockets"] = cleanup_sockets
            if cleanup_status != "SUCCEEDED":
                report["passed"] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if workflow_error:
        raise workflow_error
    if any(status != "SUCCEEDED" for status in report["cleanup"].values()):
        raise RuntimeError("任务收尾失败")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--worker-pid", type=int, required=True)
    parser.add_argument("--task-id", required=True, help="唯一的 STOPPED SSH 验证任务 ID")
    parser.add_argument("--timeout", type=int, default=300, help="等待 OFFLINE 后恢复的最大秒数")
    parser.add_argument("--reboot-confirm", action="store_true", help="确认向指定设备发送 reboot 命令")
    parser.add_argument("--allow-shared-endpoint", action="store_true",
                        help="允许其它可见任务共享端点，以活动任务数解释 socket 总量")
    asyncio.run(execute(parser.parse_args()))
