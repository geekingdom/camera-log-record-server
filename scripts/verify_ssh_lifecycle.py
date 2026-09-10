"""用正式 API 和本机套接字快照验证显式指定任务的 SSH 生命周期。

本工具不会额外连接设备，也不输出凭据或设备正文。暂停超过无日志超时时间，
确认不会重试；继续后运行 ID 保持、会话 ID 改变，并检查每设备至多一个连接。
"""
import argparse
import asyncio
import json
import subprocess
import time

import httpx
from camera_logs.common.config import Settings

INITIAL_COMMANDS = (
    "outputClose",
    "outputOpen",
    "setDebug -m all -l 7 -d 111",
    "prtHardInfo",
)
ACTIVE_STATUSES = {
    "PENDING", "CONNECTING", "COLLECTING", "RECONNECTING", "PAUSING", "STOPPING",
}


def connection_counts(pid, tasks):
    """仅检查指定采集进程的设备 SSH socket，不触碰设备其他连接。"""
    result = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:ESTABLISHED"],
                            capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError("无法读取采集进程 socket")
    return {task["id"]: sum(f'->{task["ip"]}:{task["port"]} ' in line
                           for line in result.stdout.splitlines()) for task in tasks}


def validate_task(task):
    """校验实机验证任务的 SSH、停止态和固定初始化配置，拒绝临时改写任务。"""
    if task.get("protocol") != "SSH":
        raise ValueError("任务不是 SSH 协议")
    try:
        port = int(task.get("port"))
    except (TypeError, ValueError) as error:
        raise ValueError("任务 SSH 端口无效") from error
    if port != 22:
        raise ValueError("任务 SSH 端口必须为 22")
    if not isinstance(task.get("ip"), str) or not task["ip"].strip():
        raise ValueError("任务缺少 SSH 端点 IP")
    if task.get("status") != "STOPPED" or task.get("desiredState") != "STOPPED":
        raise ValueError("任务必须处于 STOPPED 状态")
    if task.get("nodeId") is not None:
        raise ValueError("任务仍被采集节点占用")
    if task.get("enableCoredumpMonitor", False) is not False:
        raise ValueError("任务必须禁用 coredump 监控")
    if task.get("scheduledCommands"):
        raise ValueError("任务不能包含定时命令")
    actual_commands = []
    for command in task.get("initialCommands", []):
        try:
            actual_commands.append((command["command"], float(command.get("delaySeconds", 0))))
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("任务初始化命令格式无效") from error
    expected_commands = [(command, 0.3) for command in INITIAL_COMMANDS]
    if actual_commands != expected_commands:
        raise ValueError("任务初始化命令顺序或延迟不符合验证约定")


def select_tasks(all_tasks, task_ids):
    """只选择显式任务 ID，并在控制 API 调用前检查同端点活动采集。"""
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("--task-id 不能重复")
    tasks_by_id = {task.get("id"): task for task in all_tasks}
    missing_ids = [task_id for task_id in task_ids if task_id not in tasks_by_id]
    if missing_ids:
        raise ValueError("未找到显式指定任务: " + ", ".join(missing_ids))
    selected_tasks = [tasks_by_id[task_id] for task_id in task_ids]
    for task in selected_tasks:
        validate_task(task)
        for other in all_tasks:
            if other.get("id") == task["id"]:
                continue
            if other.get("ip") != task.get("ip") or other.get("port") != task.get("port"):
                continue
            if other.get("nodeId") is not None or other.get("status") in ACTIVE_STATUSES:
                raise ValueError(f"任务 {task['id']} 的端点已有其他活动任务: {other.get('id')}")
    return selected_tasks


async def load_tasks_for_validation(client, task_ids):
    """按显式 ID 读取目标，再按 IP 分页读取所有可能共享端点的任务。"""
    selected_tasks = []
    for task_id in task_ids:
        response = await client.get(f"/api/v1/tasks/{task_id}")
        response.raise_for_status()
        selected_tasks.append(response.json())
    endpoint_tasks = {task["id"]: task for task in selected_tasks}
    for ip in {task.get("ip") for task in selected_tasks}:
        page = 1
        received = 0
        while True:
            response = await client.get("/api/v1/tasks", params={"search": ip, "page": page, "pageSize": 100})
            response.raise_for_status()
            result = response.json()
            items = result["items"]
            endpoint_tasks.update({task["id"]: task for task in items})
            received += len(items)
            if received >= result["total"] or not items:
                break
            page += 1
    return select_tasks(list(endpoint_tasks.values()), task_ids)


async def wait_operation(client, operation_id, timeout=120, poll_interval=.5, on_poll=None):
    """等待正式控制 API 的异步操作结束，并在轮询时执行可选安全检查。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if on_poll is not None:
            on_poll()
        response = await client.get("/api/v1/operations/" + operation_id)
        response.raise_for_status()
        operation = response.json()
        if operation["status"] == "PENDING":
            await asyncio.sleep(poll_interval)
            continue
        if operation["status"] != "SUCCEEDED":
            raise RuntimeError("异步控制操作失败: " + operation["status"])
        return operation
    raise TimeoutError("异步控制操作未完成")


async def cleanup_tasks(client, task_ids, worker_pid, poll_interval=.5):
    """逐个停止显式任务，并确认任务状态和设备 SSH socket 都已完成回收。"""
    results = {}
    for task_id in task_ids:
        try:
            response = await client.post(f"/api/v1/tasks/{task_id}/stop")
            response.raise_for_status()
            await wait_operation(client, response.json()["id"], poll_interval=poll_interval)
            task_response = await client.get(f"/api/v1/tasks/{task_id}")
            task_response.raise_for_status()
            task = task_response.json()
            if task.get("status") != "STOPPED" or task.get("nodeId") is not None:
                raise RuntimeError("任务停止后仍未完成节点回收")
            if connection_counts(worker_pid, [task])[task_id] != 0:
                raise RuntimeError("任务停止后仍保留 SSH 连接")
            results[task_id] = "SUCCEEDED"
        except Exception as error:  # noqa: BLE001 - 收尾必须继续，汇总结果交由调用方判定。
            results[task_id] = type(error).__name__
    return results


def record_cleanup_result(report, cleanup):
    """将收尾结果写入报告；任一任务未回收完成时本轮验证判定为失败。"""
    report["cleanup"] = cleanup
    failed_tasks = [task_id for task_id, status in cleanup.items() if status != "SUCCEEDED"]
    if failed_tasks:
        report["passed"] = False
        report.setdefault("error", {"type": "CleanupError", "message": "任务收尾失败: " + ", ".join(failed_tasks)})


async def execute(args):
    """顺序执行启动、暂停、等待和继续，并始终经 stop API 回收连接。"""
    report = {"passed": False, "taskIds": args.task_id, "cycles": [], "cleanup": {}}
    workflow_error = None
    async with httpx.AsyncClient(base_url=args.url,
            headers={"Authorization": "Bearer " + Settings().bootstrap_token}, timeout=30) as client:
        tasks = await load_tasks_for_validation(client, args.task_id)

        async def control(task, action):
            """轮询异步操作，同时验证不会因重连重叠耗尽设备会话槽。"""
            response = await client.post(f'/api/v1/tasks/{task["id"]}/{action}')
            response.raise_for_status()
            operation_id = response.json()["id"]
            def check_connections():
                """在每个操作状态轮询前确认单端点没有重叠连接。"""
                counts = connection_counts(args.worker_pid, tasks)
                assert max(counts.values()) <= 1, "检测到同设备多个采集连接"
            await wait_operation(client, operation_id, on_poll=check_connections)
            return (await client.get('/api/v1/tasks/' + task["id"])).json()

        try:
            for index, task in enumerate(tasks):
                tasks[index] = await control(task, "start")
            before = {task["id"]: (task["runId"], task["sessionId"]) for task in tasks}
            for cycle in range(args.cycles):
                for task in tasks:
                    paused = await control(task, "pause")
                    assert paused["status"] == "PAUSED"
                assert all(count == 0 for count in connection_counts(args.worker_pid, tasks).values())
                # 超过十秒看门狗，证明暂停状态不会被无日志重连逻辑唤醒。
                await asyncio.sleep(12)
                assert all(count == 0 for count in connection_counts(args.worker_pid, tasks).values())
                for index, task in enumerate(tasks):
                    resumed = await control(task, "resume")
                    assert resumed["runId"] == before[task["id"]][0]
                    assert resumed["sessionId"] != before[task["id"]][1]
                    before[task["id"]] = (resumed["runId"], resumed["sessionId"])
                    tasks[index] = resumed
                counts = connection_counts(args.worker_pid, tasks)
                assert all(count == 1 for count in counts.values())
                report["cycles"].append({"cycle": cycle + 1, "pauseReleased": True,
                    "pausedRetryDisabled": True, "sameRun": True, "newSession": True,
                    "activeConnections": counts})
            report["passed"] = True
        except Exception as error:  # noqa: BLE001 - finally 仍须运行正式 stop 收尾。
            workflow_error = error
            report["error"] = {"type": type(error).__name__, "message": str(error)}
        finally:
            record_cleanup_result(report, await cleanup_tasks(client, args.task_id, args.worker_pid))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if workflow_error:
        raise workflow_error
    if any(status != "SUCCEEDED" for status in report["cleanup"].values()):
        raise RuntimeError("任务收尾失败")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--worker-pid", type=int, required=True)
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--task-id", action="append", required=True,
                        help="要验证的 STOPPED SSH 任务 ID，可重复指定")
    asyncio.run(execute(parser.parse_args()))
