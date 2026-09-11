"""用正式 API 和本机套接字快照验证显式指定任务的 SSH 生命周期。

本工具不会额外连接设备，也不输出凭据或设备正文。暂停超过无日志超时时间，
确认不会重试；继续后运行 ID 保持、会话 ID 改变，并检查每设备至多一个连接。
"""
import argparse
import asyncio
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from camera_logs.collection.ssh_admission import normalize_ssh_address
from camera_logs.common.config import Settings
from pymongo import AsyncMongoClient

INITIAL_COMMANDS = (
    "outputClose",
    "outputOpen",
    "setDebug -m all -l 7 -d 111",
    "prtHardInfo",
)
ACTIVE_STATUSES = {
    "PENDING", "CONNECTING", "COLLECTING", "RECONNECTING", "PAUSING", "STOPPING",
}


def validate_arguments(args):
    """拒绝空生命周期循环，避免只启动和收尾就被报告为验证通过。"""
    if not isinstance(args.cycles, int) or isinstance(args.cycles, bool) or args.cycles < 1:
        raise ValueError("--cycles 必须是正整数")


class SshSlotObserver:
    """只读观察当前平台库中的精确 SSH 占位，不参与设备连接或回收。"""

    def __init__(self, settings):
        self.client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
        self.database = self.client[settings.database_name]

    async def count(self, task):
        """按任务、运行和代次统计名额，旧会话不得被误算为当前连接。"""
        slot = await self.database.ssh_connection_slots.find_one(
            {"_id": normalize_ssh_address(task["ip"])}, {"claims": 1}
        )
        return sum(
            claim.get("taskId") == str(task.get("id"))
            and claim.get("runId") == str(task.get("runId"))
            and claim.get("generation") == task.get("generation")
            for claim in (slot or {}).get("claims", [])
        )

    async def count_task(self, task):
        """统计该任务在同一设备上的全部运行代次，供停止后遗留检查使用。"""
        slot = await self.database.ssh_connection_slots.find_one(
            {"_id": normalize_ssh_address(task["ip"])}, {"claims": 1}
        )
        return sum(claim.get("taskId") == str(task.get("id")) for claim in (slot or {}).get("claims", []))

    async def idle_timeout_recorded(self, task, session_id, started_at):
        """只读确认本次命令后的旧会话确实触发了持久化空闲超时事件。"""
        event = await self.database.events.find_one({
            "type": "IDLE_TIMEOUT", "taskId": str(task["id"]), "runId": str(task["runId"]),
            "sessionId": str(session_id), "createdAt": {"$gte": started_at},
        }, {"_id": 1})
        return event is not None

    async def close(self):
        """关闭只读 Mongo 客户端，不影响 Worker 或其名额文档。"""
        await self.client.close()


async def assert_slot_counts(observer, tasks, expected):
    """断言每个当前任务运行的精确占位数，输出不含任务凭据。"""
    actual = {task["id"]: await observer.count(task) for task in tasks}
    mismatched = {identifier: count for identifier, count in actual.items() if count != expected}
    if mismatched:
        raise AssertionError(f"SSH 名额数不符合预期 expected={expected} actual={mismatched}")
    return actual


async def assert_task_slots_released(observer, task):
    """停止后任务任何旧 run/generation 的遗留占位都属于失败，不能只检查新快照。"""
    count = await observer.count_task(task)
    if count:
        raise AssertionError(f"SSH 名额仍残留 taskId={task['id']} count={count}")


async def assert_idle_timeout_evidence(observer, task, old_session_id, started_at):
    """缺少旧会话的本次 IDLE_TIMEOUT 记录时，禁止把重连归因为空闲看门狗。"""
    if not await observer.idle_timeout_recorded(task, old_session_id, started_at):
        raise AssertionError("未找到本次 outputClose 后旧会话的 IDLE_TIMEOUT 事件")


def connection_counts(pid, tasks, *, established=False):
    """默认检查全部 TCP FD，不能把正在关闭的连接误当作已经释放。"""
    command = ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP"]
    if established:
        command.extend(["-sTCP:ESTABLISHED"])
    result = subprocess.run(command,
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
            if other.get("ip") != task.get("ip"):
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


async def cleanup_tasks(client, task_ids, worker_pid, poll_interval=.5, slot_observer=None):
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
            if (task.get("status") != "STOPPED" or task.get("desiredState") != "STOPPED"
                    or task.get("nodeId") is not None):
                raise RuntimeError("任务停止后仍未完成节点回收")
            if connection_counts(worker_pid, [task])[task_id] != 0:
                raise RuntimeError("任务停止后仍保留 SSH 连接")
            if slot_observer is not None:
                await assert_task_slots_released(slot_observer, task)
            results[task_id] = "SUCCEEDED"
        except Exception as error:  # noqa: BLE001 - 收尾必须继续，汇总结果交由调用方判定。
            results[task_id] = type(error).__name__
    return results


async def verify_idle_reconnect(client, task, worker_pid, slot_observer, timeout=45):
    """仅在显式开关下关闭设备输出，验证空闲重连保持运行并重新取得一个名额。"""
    before = (task["runId"], task["sessionId"])
    started_at = datetime.now(UTC)
    try:
        os.kill(worker_pid, 0)
    except OSError as error:
        raise RuntimeError("采集 Worker 进程不存在，拒绝解释空闲重连") from error
    response = await client.post(f'/api/v1/tasks/{task["id"]}/commands', json={"command": "outputClose"},
                                 headers={"Idempotency-Key": "ssh-idle-close-" + uuid4().hex})
    response.raise_for_status()
    command_id = response.json()["id"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        current = await client.get(f"/api/v1/commands/{command_id}")
        current.raise_for_status()
        status = current.json().get("status")
        if status == "SENT":
            break
        if status in {"FAILED", "CANCELLED", "UNKNOWN"}:
            raise RuntimeError("outputClose 未进入发送路径: " + status)
        await asyncio.sleep(.5)
    else:
        raise TimeoutError("等待 outputClose 命令发送超时")

    # Worker 无输出看门狗至少10秒。全 TCP FD 连续采样保证没有观察到两路并存；
    # 采样不能排除采样间极短重叠，也无法区分其它断线原因，报告必须保留该边界。
    quiet_deadline, samples = time.monotonic() + 11, []
    while time.monotonic() < quiet_deadline:
        count = connection_counts(worker_pid, [task], established=False)[task["id"]]
        if count > 1:
            raise AssertionError(f"空闲窗口出现多个 SSH TCP FD count={count}")
        samples.append(count)
        await asyncio.sleep(.2)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = await client.get(f'/api/v1/tasks/{task["id"]}')
        current.raise_for_status()
        recovered = current.json()
        sockets = connection_counts(worker_pid, [recovered])[recovered["id"]]
        if sockets > 1:
            raise AssertionError("重连阶段观察到多个 SSH TCP FD")
        if (recovered.get("status") == "COLLECTING" and recovered.get("runId") == before[0]
                and recovered.get("sessionId") != before[1] and recovered.get("nodeId") == Settings().node_id
                and sockets == 1):
            await assert_slot_counts(slot_observer, [recovered], 1)
            await assert_idle_timeout_evidence(slot_observer, recovered, before[1], started_at)
            return {"taskId": recovered["id"], "sameRun": True, "newSession": True,
                    "slotCount": 1, "endpointSockets": sockets, "quietSamples": len(samples),
                    "idleTimeoutSessionId": before[1],
                    "initialOutputOpenConfigured": "outputOpen" in INITIAL_COMMANDS,
                    "idleCauseLimit": "已核对本次旧会话的空闲超时事件；FD采样仍不能排除采样间短暂连接重叠"}
        await asyncio.sleep(.5)
    raise TimeoutError("无输出重连后未观察到同运行的新会话")


def record_cleanup_result(report, cleanup):
    """将收尾结果写入报告；任一任务未回收完成时本轮验证判定为失败。"""
    report["cleanup"] = cleanup
    failed_tasks = [task_id for task_id, status in cleanup.items() if status != "SUCCEEDED"]
    if failed_tasks:
        report["passed"] = False
        report.setdefault("error", {"type": "CleanupError", "message": "任务收尾失败: " + ", ".join(failed_tasks)})


async def execute(args):
    """顺序执行启动、暂停、等待和继续，并始终经 stop API 回收连接。"""
    validate_arguments(args)
    report = {"passed": False, "taskIds": args.task_id, "cycles": [], "cleanup": {}}
    workflow_error = None
    settings = Settings()
    slot_observer = SshSlotObserver(settings)
    try:
        async with httpx.AsyncClient(base_url=args.url,
                                    headers={"Authorization": "Bearer " + settings.bootstrap_token}, timeout=30) as client:
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
                current = await client.get('/api/v1/tasks/' + task["id"])
                current.raise_for_status()
                return current.json()

            try:
                os.kill(args.worker_pid, 0)
                for index, task in enumerate(tasks):
                    tasks[index] = await control(task, "start")
                    if tasks[index].get("nodeId") != settings.node_id:
                        raise AssertionError("任务未分配到本机配置的Worker，不能使用本机PID验证")
                if any(count != 1 for count in connection_counts(args.worker_pid, tasks).values()):
                    raise AssertionError("启动后指定Worker未持有预期SSH连接")
                await assert_slot_counts(slot_observer, tasks, 1)
                before = {task["id"]: (task["runId"], task["sessionId"]) for task in tasks}
                for cycle in range(args.cycles):
                    for task in tasks:
                        paused = await control(task, "pause")
                        assert paused["status"] == "PAUSED"
                    assert all(count == 0 for count in connection_counts(args.worker_pid, tasks).values())
                    await assert_slot_counts(slot_observer, tasks, 0)
                    await asyncio.sleep(12)
                    assert all(count == 0 for count in connection_counts(args.worker_pid, tasks).values())
                    await assert_slot_counts(slot_observer, tasks, 0)
                    for index, task in enumerate(tasks):
                        resumed = await control(task, "resume")
                        assert resumed["runId"] == before[task["id"]][0]
                        assert resumed["sessionId"] != before[task["id"]][1]
                        before[task["id"]] = (resumed["runId"], resumed["sessionId"])
                        tasks[index] = resumed
                    counts = connection_counts(args.worker_pid, tasks)
                    assert all(count == 1 for count in counts.values())
                    await assert_slot_counts(slot_observer, tasks, 1)
                    report["cycles"].append({"cycle": cycle + 1, "pauseReleased": True,
                        "pausedRetryDisabled": True, "sameRun": True, "newSession": True,
                        "activeConnections": counts})
                if getattr(args, "verify_idle_reconnect", False):
                    report["idleReconnect"] = []
                    for index, task in enumerate(tasks):
                        report["idleReconnect"].append(await verify_idle_reconnect(
                            client, task, args.worker_pid, slot_observer
                        ))
                        response = await client.get(f'/api/v1/tasks/{task["id"]}')
                        response.raise_for_status()
                        tasks[index] = response.json()
                report["passed"] = True
            except Exception as error:  # noqa: BLE001 - finally 仍须运行正式 stop 收尾。
                workflow_error = error
                report["error"] = {"type": type(error).__name__, "message": str(error)}
            finally:
                record_cleanup_result(report, await cleanup_tasks(
                    client, args.task_id, args.worker_pid, slot_observer=slot_observer
                ))
    finally:
        await slot_observer.close()
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
    parser.add_argument("--verify-idle-reconnect", action="store_true",
                        help="显式发送 outputClose 并验证超过10秒无输出后的同运行重连")
    parser.add_argument("--task-id", action="append", required=True,
                        help="要验证的 STOPPED SSH 任务 ID，可重复指定")
    asyncio.run(execute(parser.parse_args()))
