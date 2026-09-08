"""使用本地合成串口验证资源、同端口双任务、软删除与日志下载，可附带浏览器验收。"""

import argparse
import asyncio
import io
import json
import os
import re
import tarfile
from uuid import uuid4

import httpx
import telnetlib3
from camera_logs.common.config import Settings


async def wait_for(client, path, predicate):
    """有界等待持久化任务或下载状态，失败时不输出设备凭据。"""
    async with asyncio.timeout(45):
        while True:
            response = await client.get(path)
            response.raise_for_status()
            data = response.json()
            if predicate(data):
                return data
            await asyncio.sleep(.2)


async def run(url, browser):
    """真实调用当前开发 API，设备侧完全由本地 Telnet 模拟器承担。"""
    connections = set()
    connected_count = 0

    async def shell(reader, writer):
        nonlocal connected_count
        connected_count += 1
        task = asyncio.current_task()
        connections.add(task)

        async def emit():
            number = 0
            while True:
                writer.write(f"resource-workflow seq={number:06d}\n".encode())
                await writer.drain()
                number += 1
                await asyncio.sleep(.1)

        emitter = asyncio.create_task(emit())
        try:
            while await reader.read(4096):
                pass
        finally:
            emitter.cancel()
            await asyncio.gather(emitter, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            connections.discard(task)

    server = await telnetlib3.create_server(host="127.0.0.1", port=0, shell=shell,
                                          encoding=False, connect_maxwait=.05, timeout=False)
    port = server.sockets[0].getsockname()[1]
    settings = Settings()
    headers = {"Authorization": "Bearer " + settings.bootstrap_token}
    tasks = []
    resource = None
    async with httpx.AsyncClient(base_url=url.rstrip("/") + "/api/v1", headers=headers, timeout=30) as client:
        async def create(path, body):
            response = await client.post(path, json=body, headers={"Idempotency-Key": uuid4().hex})
            response.raise_for_status()
            return response.json()

        try:
            resource = await create("/resources", {"name": "资源流程合成验收", "kind": "SERIAL_SERVER", "ip": "127.0.0.1"})
            for number in range(2):
                task = await create("/tasks", {"name": f"资源流程合成任务-{number+1}", "resourceId": resource["id"],
                    "protocol": "TELNET_SERIAL", "ip": "127.0.0.1", "port": port, "autoStart": True})
                tasks.append(task)
            for task in tasks:
                await wait_for(client, f'/tasks/{task["id"]}', lambda item: item["status"] == "COLLECTING")
                await wait_for(client, f'/tasks/{task["id"]}/log-hours', lambda item: bool(item["items"]))
            assert connected_count == 2, "同端口任务未形成两个独立连接"
            if browser:
                process = await asyncio.create_subprocess_exec("node", "scripts/browser_smoke.mjs", env=os.environ.copy())
                try:
                    async with asyncio.timeout(180):
                        assert await process.wait() == 0, "浏览器验收失败"
                finally:
                    if process.returncode is None:
                        process.terminate()
                        await process.wait()
            # 删除时两路仍在采集，验证后端通过原生命周期关闭连接且保留归档。
            response = await client.delete(f'/resources/{resource["id"]}', params={"version": resource["version"]})
            response.raise_for_status()
            assert response.json()["deletedAt"]
            for task in tasks:
                await wait_for(client, f'/tasks/{task["id"]}', lambda item: item["status"] == "STOPPED" and not item.get("nodeId"))
                assert (await client.post(f'/tasks/{task["id"]}/start')).status_code == 409
                hours = await wait_for(client, f'/tasks/{task["id"]}/log-hours',
                    lambda item: bool(item["items"]) and all(hour["status"] == "READY" for hour in item["items"]))
                sequence = []
                for hour in sorted(hours["items"], key=lambda item: item["hourId"]):
                    job = await create("/downloads", {"taskId": task["id"], "hourIds": [hour["hourId"]]})
                    await wait_for(client, f'/downloads/{job["id"]}', lambda item: item["status"] == "SUCCEEDED")
                    download = await client.get(f'/downloads/{job["id"]}/content')
                    download.raise_for_status()
                    with tarfile.open(fileobj=io.BytesIO(download.content), mode="r:gz") as bundle:
                        members = [member for member in bundle.getmembers() if member.name.endswith(".log")]
                        assert len(members) == 1
                        raw = bundle.extractfile(members[0]).read()
                        sequence.extend(int(value) for value in re.findall(rb"resource-workflow seq=(\d+)", raw))
                assert sequence and sequence == list(range(len(sequence))), "采集顺序或完整性不一致"
            assert not connections, "资源删除后仍残留采集连接"
            await wait_for(client, f'/resources/{resource["id"]}',
                           lambda item: item.get("deletionState") == "DONE")
            print(json.dumps({"passed": True, "samePortTasks": 2, "softDeleted": True,
                              "logsDownloadable": True, "connectionsClosed": True, "resourceId": resource["id"]}))
        finally:
            for task in tasks:
                await client.post(f'/tasks/{task["id"]}/stop')
            server.close()
            await server.wait_closed()
            for connection in list(connections):
                connection.cancel()
            await asyncio.gather(*connections, return_exceptions=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.url, args.browser))
