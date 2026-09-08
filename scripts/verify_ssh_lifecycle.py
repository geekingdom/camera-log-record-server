"""用正式 API 和本机套接字快照验证两台联调设备的 SSH 生命周期。

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


def connection_counts(pid, tasks):
    """仅检查指定采集进程的设备 SSH socket，不触碰设备其他连接。"""
    result = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:ESTABLISHED"],
                            capture_output=True, text=True, check=False)
    if result.returncode not in (0, 1):
        raise RuntimeError("无法读取采集进程 socket")
    return {task["id"]: sum(f'->{task["ip"]}:{task["port"]} ' in line
                           for line in result.stdout.splitlines()) for task in tasks}


async def execute(args):
    """顺序执行启动、暂停、等待、继续，保留采集任务供开发控制台查看。"""
    async with httpx.AsyncClient(base_url=args.url,
            headers={"Authorization": "Bearer " + Settings().bootstrap_token}, timeout=30) as client:
        response = await client.get("/api/v1/tasks", params={"pageSize": 100})
        response.raise_for_status()
        tasks = [task for task in response.json()["items"] if task["name"].startswith("联调设备-")]
        assert len(tasks) == 2, "联调设备任务数量不符"

        async def control(task, action):
            """轮询异步操作，同时验证不会因重连重叠耗尽设备会话槽。"""
            response = await client.post(f'/api/v1/tasks/{task["id"]}/{action}')
            response.raise_for_status()
            operation_id = response.json()["id"]
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                counts = connection_counts(args.worker_pid, tasks)
                assert max(counts.values()) <= 1, "检测到同设备多个采集连接"
                operation = (await client.get("/api/v1/operations/" + operation_id)).json()
                if operation["status"] != "PENDING":
                    assert operation["status"] == "SUCCEEDED", operation["status"]
                    return (await client.get('/api/v1/tasks/' + task["id"])).json()
                await asyncio.sleep(.5)
            raise TimeoutError("异步控制操作未完成")

        for index, task in enumerate(tasks):
            tasks[index] = await control(task, "resume" if task["status"] == "PAUSED" else "start")
        before = {task["id"]: (task["runId"], task["sessionId"]) for task in tasks}
        report = []
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
            report.append({"cycle": cycle + 1, "pauseReleased": True, "pausedRetryDisabled": True,
                           "sameRun": True, "newSession": True, "activeConnections": counts})
        print(json.dumps({"passed": True, "cycles": report}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--worker-pid", type=int, required=True)
    parser.add_argument("--cycles", type=int, default=2)
    asyncio.run(execute(parser.parse_args()))
