"""验证 SSH 任务在设备重启期间保持暂停，离线后由显式继续恢复。

必须提供 ``--reboot-confirm``。脚本不创建资源、不建立额外 SSH 连接，只使用正式 API
向当前采集会话发送 reboot，并在 finally 中停止该显式测试任务。
"""

import argparse
import asyncio
import json
import os
import time
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from verify_ssh_lifecycle import (
    ACTIVE_STATUSES,
    SshSlotObserver,
    cleanup_tasks,
    connection_counts,
    load_tasks_for_validation,
    wait_operation,
)
from verify_ssh_reboot_recovery import (
    get_resource,
    get_task,
    list_tasks,
    public_resource_state,
    public_task_state,
    start_and_wait_collecting,
    wait_command_sent,
)

POLL_SECONDS = 0.5
MINIMUM_PAUSED_SECONDS = 65


def validate_arguments(args):
    """限制一条显式任务和足够长的真实离线观察窗口。"""
    if not isinstance(args.task_id, list) or len(args.task_id) != 1 or not args.task_id[0]:
        raise ValueError("必须且只能提供一个 --task-id")
    if not MINIMUM_PAUSED_SECONDS + 5 <= args.timeout <= 420:
        raise ValueError("--timeout 必须在70至420秒之间")


def validate_reboot_resource(task, resource):
    """重启前确认任务仍绑定未删除的海康网络资源，且端点没有漂移。"""
    if resource.get("kind") != "HIKVISION_NETWORK":
        raise ValueError("暂停重启验证只支持 HIKVISION_NETWORK 资源")
    if resource.get("deletedAt") is not None:
        raise ValueError("验证资源已删除")
    if resource.get("ip") != task.get("ip"):
        raise ValueError("验证资源 IP 与任务端点不一致")


async def assert_no_shared_active_ip(client, task):
    """每次关键状态轮询均拒绝同 IP 的其它活动任务，避免误解释 TCP FD。"""
    tasks = await list_tasks(client, {"search": task["ip"]})
    active = [item["id"] for item in tasks if item.get("id") != task["id"]
              and item.get("ip") == task["ip"]
              and (item.get("nodeId") is not None or item.get("status") in ACTIVE_STATUSES)]
    if active:
        raise RuntimeError("同一 IP 存在其它活动任务: " + ", ".join(active))


async def assert_paused_snapshot(observer, task, run_id, worker_pid):
    """暂停期只能保持原运行，且必须已经释放本机 SSH FD 与数据库名额。"""
    if task.get("status") != "PAUSED" or task.get("desiredState") != "PAUSED":
        raise AssertionError("离线等待期间任务不再保持 PAUSED")
    if task.get("runId") != run_id or task.get("nodeId") is not None:
        raise AssertionError("暂停任务运行身份或节点归属发生变化")
    fds = connection_counts(worker_pid, [task])[task["id"]]
    if fds != 0:
        raise AssertionError(f"暂停任务仍有 SSH FD count={fds}")
    slots = await observer.count(task)
    if slots != 0:
        raise AssertionError(f"暂停任务仍有 SSH 名额 count={slots}")


async def pause_after_reboot_command(client, task_id):
    """在 reboot 已进入发送路径后立刻请求暂停，不等待资源探测先标记 OFFLINE。"""
    response = await client.post(f"/api/v1/tasks/{task_id}/pause")
    response.raise_for_status()
    return await wait_operation(client, response.json()["id"], poll_interval=POLL_SECONDS)


def pause_window_satisfied(paused_seconds, offline_observed_at, health_status):
    """判断暂停窗口是否达标；返回窗口结果及仅代表观测范围的 OFFLINE 秒数。"""
    observed_seconds = max(0, paused_seconds - offline_observed_at) if offline_observed_at is not None else 0
    return (paused_seconds >= MINIMUM_PAUSED_SECONDS and offline_observed_at is not None
            and health_status == "OFFLINE"), observed_seconds


async def observe_offline_pause(client, task_id, resource_id, run_id, worker_pid, observer, timeout, paused_started):
    """从暂停完成开始计时，窗口满65秒且当前仍 OFFLINE、曾观测 OFFLINE 才通过。"""
    deadline, offline_observed_at, last = paused_started + timeout, None, None
    transitions = []
    while time.monotonic() < deadline:
        task, resource = await asyncio.gather(get_task(client, task_id), get_resource(client, resource_id))
        await assert_no_shared_active_ip(client, task)
        await assert_paused_snapshot(observer, task, run_id, worker_pid)
        state = (task.get("status"), task.get("desiredState"), resource.get("healthStatus"))
        if state != last:
            transitions.append({"atSeconds": round(time.monotonic() - paused_started, 3),
                                "task": public_task_state(task), "resource": public_resource_state(resource)})
            last = state
        paused_seconds = time.monotonic() - paused_started
        if resource.get("healthStatus") == "OFFLINE":
            offline_observed_at = offline_observed_at if offline_observed_at is not None else paused_seconds
        elif offline_observed_at is not None and paused_seconds < MINIMUM_PAUSED_SECONDS:
            raise RuntimeError("设备在暂停满65秒前从 OFFLINE 恢复，未满足长重启验收条件")
        satisfied, observed_seconds = pause_window_satisfied(
            paused_seconds, offline_observed_at, resource.get("healthStatus"))
        if satisfied:
            return task, resource, round(paused_seconds, 3), round(observed_seconds, 3), transitions
        await asyncio.sleep(POLL_SECONDS)
    if offline_observed_at is None:
        raise TimeoutError("未观察到设备资源 OFFLINE")
    raise TimeoutError("暂停窗口内未在满65秒时观察到资源仍为 OFFLINE")


async def request_resume_while_offline(client, task_id, resource_id, run_id, worker_pid, observer):
    """在资源仍 OFFLINE 时提交继续；接受 WAITING_DEVICE 或后续连接过渡，但拒绝错误终态。"""
    resource = await get_resource(client, resource_id)
    if resource.get("healthStatus") != "OFFLINE":
        raise RuntimeError("请求 continue 前资源已不处于 OFFLINE，未满足验收前提")
    response = await client.post(f"/api/v1/tasks/{task_id}/resume")
    response.raise_for_status()
    if response.status_code != 202:
        raise AssertionError("resume 未返回202")
    operation_id = response.json()["id"]
    task = await get_task(client, task_id)
    if task.get("desiredState") != "RUNNING" or task.get("runId") != run_id:
        raise RuntimeError("离线 resume 未保留原运行或未建立 RUNNING 意图")
    if task.get("status") not in {"WAITING_DEVICE", "PENDING", "CONNECTING", "COLLECTING"}:
        raise RuntimeError("离线 resume 后进入错误终态")
    if task.get("status") == "WAITING_DEVICE":
        if task.get("nodeId") is not None:
            raise AssertionError("WAITING_DEVICE 任务不应持有采集节点")
        fds = connection_counts(worker_pid, [task])[task["id"]]
        slots = await observer.count(task)
        if fds != 0 or slots != 0:
            raise AssertionError("WAITING_DEVICE 任务不应持有 SSH FD 或名额")
    return operation_id, task


async def wait_resume_collecting(client, task_id, resource_id, run_id, old_session, worker_pid, observer, timeout):
    """等待资源重新 ONLINE、原运行的新会话采集，以及继续操作最终成功。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task, resource = await asyncio.gather(get_task(client, task_id), get_resource(client, resource_id))
        await assert_no_shared_active_ip(client, task)
        if task.get("status") in {"ERROR", "BLOCKED", "STOPPED"}:
            raise RuntimeError("恢复期间任务进入错误终态")
        if (resource.get("healthStatus") == "ONLINE" and task.get("status") == "COLLECTING"
                and task.get("desiredState") == "RUNNING" and task.get("runId") == run_id
                and task.get("sessionId") and task.get("sessionId") != old_session
                and task.get("nodeId") == Settings().node_id):
            fds = connection_counts(worker_pid, [task])[task["id"]]
            if fds != 1:
                raise AssertionError(f"恢复采集的 SSH FD 数不为1 count={fds}")
            slots = await observer.count(task)
            if slots != 1:
                raise AssertionError(f"恢复采集的 SSH 名额数不为1 count={slots}")
            return task, resource
        await asyncio.sleep(POLL_SECONDS)
    raise TimeoutError("等待资源 ONLINE 和原运行新会话 COLLECTING 超时")


async def execute(args):
    """执行显式重启、立即暂停、真实离线观察、离线继续及最终受控停止。"""
    if not args.reboot_confirm:
        raise ValueError("必须显式传入 --reboot-confirm 才会向设备发送 reboot")
    validate_arguments(args)
    report = {"passed": False, "taskId": args.task_id[0], "timeoutSeconds": args.timeout, "cleanup": {}}
    settings, observer, workflow_error = Settings(), SshSlotObserver(Settings()), None
    try:
        os.kill(args.worker_pid, 0)
        async with httpx.AsyncClient(base_url=args.url, headers={"Authorization": "Bearer " + settings.bootstrap_token}, timeout=30) as client:
            task = (await load_tasks_for_validation(client, args.task_id))[0]
            resource_id = task.get("resourceId")
            if not resource_id:
                raise ValueError("任务缺少设备资源绑定")
            try:
                await assert_no_shared_active_ip(client, task)
                resource = await get_resource(client, resource_id)
                validate_reboot_resource(task, resource)
                if resource.get("healthStatus") != "ONLINE":
                    raise RuntimeError("设备资源必须先处于 ONLINE")
                started = await start_and_wait_collecting(client, task["id"], args.worker_pid)
                if started.get("nodeId") != settings.node_id:
                    raise RuntimeError("验证任务未运行在当前 Worker")
                if connection_counts(args.worker_pid, [started])[started["id"]] != 1 or await observer.count(started) != 1:
                    raise AssertionError("启动后 SSH FD 或名额数不为1")
                response = await client.post(f"/api/v1/tasks/{task['id']}/commands", json={"command": "reboot"},
                                             headers={"Idempotency-Key": "paused-reboot-" + uuid4().hex})
                response.raise_for_status()
                command = await wait_command_sent(client, response.json()["id"])
                report["rebootCommand"] = {"id": command.get("id"), "status": command.get("status")}
                await pause_after_reboot_command(client, task["id"])
                paused = await get_task(client, task["id"])
                await assert_paused_snapshot(observer, paused, started["runId"], args.worker_pid)
                paused_started = time.monotonic()
                offline_task, offline_resource, paused_seconds, observed_offline_seconds, transitions = await observe_offline_pause(
                    client, task["id"], resource_id, started["runId"], args.worker_pid, observer, args.timeout,
                    paused_started)
                operation_id, accepted = await request_resume_while_offline(
                    client, task["id"], resource_id, started["runId"], args.worker_pid, observer)
                recovered, resource = await wait_resume_collecting(
                    client, task["id"], resource_id, started["runId"], started["sessionId"], args.worker_pid, observer,
                    args.timeout)
                await wait_operation(client, operation_id, timeout=args.timeout, poll_interval=POLL_SECONDS)
                report.update(passed=True, started=public_task_state(started), paused=public_task_state(offline_task),
                              resource=public_resource_state(offline_resource), pausedSeconds=paused_seconds,
                              observedOfflineSeconds=observed_offline_seconds,
                              resumeAccepted=public_task_state(accepted), recovered=public_task_state(recovered),
                              recoveredResource=public_resource_state(resource), transitions=transitions)
            except Exception as error:  # noqa: BLE001 - 必须先完成正式 stop 收尾再重新抛出。
                workflow_error = error
                report["error"] = {"type": type(error).__name__, "message": str(error)}
            finally:
                cleanup = await cleanup_tasks(client, args.task_id, args.worker_pid, poll_interval=POLL_SECONDS,
                                              slot_observer=observer)
                report["cleanup"] = cleanup
                if any(result != "SUCCEEDED" for result in cleanup.values()):
                    report["passed"] = False
    finally:
        await observer.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if workflow_error:
        raise workflow_error
    if not report["passed"]:
        raise RuntimeError("暂停重启验证未完成")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--worker-pid", type=int, required=True)
    parser.add_argument("--task-id", action="append", required=True, help="唯一的 STOPPED SSH 验证任务 ID")
    parser.add_argument("--timeout", type=int, default=420, help="最长观察秒数，70至420")
    parser.add_argument("--reboot-confirm", action="store_true", help="确认向指定设备发送 reboot")
    asyncio.run(execute(parser.parse_args()))
