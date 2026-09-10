"""经现有采集会话触发一次设备核心转储，并观测服务器扫描到稳定文件。

脚本不建立设备 SSH 连接、不停止任务、不删除设备文件。必须提供 --trigger-core
才会提交一次 kill -6；命令输出只在内存中用于确认挂载和解析 PID，绝不打印。
"""

import argparse
import asyncio
import base64
import json
import re
import time
from uuid import uuid4

import httpx
import websockets
from camera_logs.collection.coredump_monitor import mount_target
from camera_logs.common.config import Settings


class LiveFrames:
    """跨 WebSocket 数据帧累计命令后的日志文本，避免分包切断 ps 行。"""
    def __init__(self, url, token, session_id):
        self.url, self.token, self.data = url, token, ""
        self.ready, self.closed = asyncio.Event(), asyncio.Event()
        self.session_id, self.failure = session_id, None

    async def run(self):
        """订阅既有任务实时流；坏帧丢弃而不泄露其设备正文。"""
        try:
            async with websockets.connect(self.url, max_size=None, open_timeout=10) as socket:
                await socket.send(json.dumps({"token": self.token}))
                self.ready.set()
                while True:
                    try:
                        event = json.loads(await socket.recv())
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "gap":
                        raise RuntimeError("实时日志出现 gap，无法证明命令输出连续")
                    if event.get("type") == "data":
                        if event.get("sessionId") != self.session_id:
                            raise RuntimeError("实时日志会话已变化，拒绝使用后继会话输出")
                        try:
                            self.data += base64.b64decode(event["data"], validate=True).decode(errors="replace")
                            if len(self.data) > 1024 * 1024:
                                self.data = self.data[-512 * 1024:]
                        except (KeyError, ValueError):
                            continue
        except Exception as error:  # noqa: BLE001 - 只传播协议状态，绝不保存设备正文。
            self.failure = error
        finally:
            self.closed.set()

    def reset(self):
        """仅保留 reset 之后收到的输出，避免历史实时尾帧参与 ps 匹配。"""
        self.data = ""

    def after(self, position):
        """按文本字符边界丢弃命令回显，中文日志不会使正则位置错位。"""
        self.data = self.data[position:]

    async def wait_for(self, predicate, timeout=20):
        """等待内存输出符合条件；超时仅报告条件名称，不输出设备正文。"""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.failure:
                raise self.failure
            result = predicate(self.data)
            if result:
                return result
            await asyncio.sleep(.2)
        raise TimeoutError("等待命令输出确认超时")


def websocket_url(api_url, task_id):
    """从 HTTP API 地址生成同源任务日志 WebSocket 地址。"""
    scheme = "wss" if api_url.startswith("https://") else "ws"
    return scheme + api_url.split("://", 1)[1].rstrip("/") + f"/api/v1/tasks/{task_id}/logs"


async def api(client, method, path, **kwargs):
    """调用正式 API 并统一检查 HTTP 结果。"""
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


async def command(client, task_id, session_id, text):
    """提交一次同会话手动命令，使用独立幂等键，未知结果从不重发。"""
    current_task = await api(client, "GET", f"/api/v1/tasks/{task_id}")
    if current_task.get("status") != "COLLECTING" or current_task.get("sessionId") != session_id:
        raise RuntimeError("采集会话已变化，拒绝向后继会话发送命令")
    result = await api(client, "POST", f"/api/v1/tasks/{task_id}/commands", json={"command": text},
                       headers={"Idempotency-Key": "device-coredump-" + uuid4().hex})
    identifier, deadline = result["id"], time.monotonic() + 30
    while time.monotonic() < deadline:
        current = await api(client, "GET", f"/api/v1/commands/{identifier}")
        if current.get("status") in {"SENT", "UNKNOWN"}:
            return current
        if current.get("status") in {"FAILED", "CANCELLED"}:
            raise RuntimeError("命令未进入发送路径: " + current["status"])
        await asyncio.sleep(.3)
    raise TimeoutError("等待命令发送超时")


async def all_coredumps(client, resource_id):
    """分页读取当前资源 catalog，只返回公开元数据。"""
    page, items = 1, []
    while True:
        result = await api(client, "GET", f"/api/v1/resources/{resource_id}/coredumps", params={"page": page, "pageSize": 100})
        current = result["items"]
        items.extend(current)
        if len(items) >= result["total"]:
            return items
        if not current:
            raise RuntimeError("coredump 目录分页返回空页，拒绝无限轮询")
        page += 1


def _matches_triggered_pid(name, pid):
    """只接受海康 core-6 Dsp_Main 文件名中本次 kill 的精确 PID 段。"""
    return bool(re.search(
        r"(?:^|/)[^/]*-core-6-Dsp_Main-" + re.escape(str(pid)) + r"-[^/]*$", name
    ))


async def observe_new_file(client, resource_id, baseline_ids, baseline_names, pid, triggered):
    """仅记录本次 PID 的新名字文件，持续观察到 CHANGING 后的稳定状态。"""
    await triggered.wait()
    deadline, history, seen_at, saw_changing = time.monotonic() + 180, [], None, set()
    while time.monotonic() < deadline:
        for item in await all_coredumps(client, resource_id):
            if item["id"] in baseline_ids or item.get("name") in baseline_names:
                continue
            if not _matches_triggered_pid(item.get("name", ""), pid):
                continue
            point = {key: item.get(key) for key in ("id", "name", "size", "status", "sourceState", "sourceObservedAt", "sourceStableAt")}
            if not history or point != history[-1]["file"]:
                history.append({"atSeconds": round(time.monotonic() - triggered.when, 3), "file": point})
            seen_at = seen_at or time.monotonic()
            if item.get("sourceState") == "CHANGING":
                saw_changing.add(item["id"])
            if item["id"] in saw_changing and item.get("sourceState") == "STABLE" and int(item.get("size") or 0) > 0:
                return item, round(seen_at - triggered.when, 3), history
        await asyncio.sleep(.5)
    raise TimeoutError("180 秒内未观察到新的稳定 coredump 文件")


async def execute(args):
    """预检运行会话，确认 NFS 挂载，解析唯一 DSP PID 后一次性触发并观察。"""
    if not args.trigger_core:
        raise ValueError("必须显式传入 --trigger-core 才会提交 kill -6")
    token = Settings().bootstrap_token
    report, observer = {"passed": False, "taskId": args.task_id, "triggered": False}, None
    async with httpx.AsyncClient(base_url=args.url, headers={"Authorization": "Bearer " + token}, timeout=30) as client:
        task = await api(client, "GET", f"/api/v1/tasks/{args.task_id}")
        if task.get("status") != "COLLECTING" or task.get("desiredState") != "RUNNING" or not task.get("sessionId"):
            raise RuntimeError("任务必须处于已有 COLLECTING 会话")
        resource_id = task.get("resourceId")
        if not resource_id:
            raise RuntimeError("任务缺少资源绑定")
        baseline_items = await all_coredumps(client, resource_id)
        baseline_ids = {item["id"] for item in baseline_items}
        baseline_names = {item.get("name") for item in baseline_items}
        frames = LiveFrames(websocket_url(args.url, args.task_id), token, task["sessionId"])
        stream = asyncio.create_task(frames.run())
        try:
            await asyncio.wait_for(frames.ready.wait(), 12)
            await asyncio.sleep(.5)
            _directory, target = mount_target(args.server, args.root, task["ip"])
            frames.reset()
            await command(client, args.task_id, task["sessionId"], f"gdbcfg --password=hiklinux --nfsmount={target} --open=1")
            await command(client, args.task_id, task["sessionId"], "mount")
            await frames.wait_for(lambda value: _mounted_nfs(value, target))
            frames.reset()
            await command(client, args.task_id, task["sessionId"], "ps")
            echo = await frames.wait_for(_ps_echo)
            frames.after(echo.end())
            pid = await frames.wait_for(_dsp_pid)
            triggered = asyncio.Event()
            triggered.when = time.monotonic()
            observer = asyncio.create_task(
                observe_new_file(client, resource_id, baseline_ids, baseline_names, pid, triggered)
            )
            triggered.set()
            sent = await command(client, args.task_id, task["sessionId"], f"kill -6 {pid}")
            report["triggered"], report["killCommandStatus"] = True, sent.get("status")
            triggered.set()
            found, latency, history = await observer
            report.update(passed=True, baselineCount=len(baseline_ids), firstDiscoverySeconds=latency,
                          file={key: found.get(key) for key in ("id", "name", "size", "status", "sourceState", "sourceStableAt")},
                          observations=history)
        finally:
            if observer and not observer.done():
                observer.cancel()
                await asyncio.gather(observer, return_exceptions=True)
            stream.cancel()
            await asyncio.gather(stream, return_exceptions=True)
    print(json.dumps(report, ensure_ascii=False, indent=2))


def _dsp_pid(value):
    """在后续完整输出已到达后，从完整 ps 行确认唯一 Dsp_Main PID。"""
    complete = [line.rstrip("\r\n") for line in value.splitlines(keepends=True) if line.endswith(("\n", "\r"))]
    matches = [
        match.group(1)
        for line in complete
        if (match := re.match(r"^\[[^\]\r\n]+\][ \t]*(\d+)[ \t]+.*\{Dsp_Main\}[ \t]+/home/hikdsp(?:[ \t]|$).*$", line))
    ]
    if len(set(matches)) != 1:
        if len(set(matches)) > 1:
            raise ValueError("ps 中匹配到多个 Dsp_Main /home/hikdsp PID")
        return None
    last_match = max(index for index, line in enumerate(complete) if "{Dsp_Main}" in line and "/home/hikdsp" in line)
    prompt = re.compile(r"^\[[^\]\r\n]+\][ \t]*(?:\([^\r\n)]*\))?#[ \t]*$")
    if not any(prompt.match(line) for line in complete[last_match + 1:]):
        return None
    return matches[0]


def _ps_echo(value):
    """定位服务器时间前缀后的精确 ps 命令回显，旧实时尾帧不能作为解析起点。"""
    return re.search(r"^\[[^\]\r\n]+\]\s+ps\r?$", value, re.MULTILINE)


def _mounted_nfs(value, target):
    """仅确认本次目标的完整 NFS/NFS4 挂载行，不能被普通回显冒充。"""
    return re.search(
        re.escape(target) + r" on [^\r\n]+ type nfs(?:4)?(?:[ \t(]|$)", value, re.MULTILINE
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--trigger-core", action="store_true")
    asyncio.run(execute(parser.parse_args()))
