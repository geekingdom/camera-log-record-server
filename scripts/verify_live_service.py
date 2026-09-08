"""验证真实服务的实时日志、命令、归档与 Range 下载；不输出敏感内容。"""
import argparse
import asyncio
import base64
import json
import time
import uuid

import httpx
import websockets
from camera_logs.common.config import Settings


async def verify(url, stop_after, name_prefix="联调设备-"):
    """对本地标记的联调设备执行端到端只读和受控停止校验。"""
    settings = Settings()
    headers = {"Authorization": "Bearer "+settings.bootstrap_token}
    report = {"checkedAt": time.time(), "tasks": []}
    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=120) as client:
        tasks = (await client.get("/api/v1/tasks")).json()["items"]
        tasks = [t for t in tasks if t["name"].startswith(name_prefix)]
        if not tasks:
            raise RuntimeError("未找到指定前缀的联调任务，不能视为验收通过")
        for task in tasks:
            task_id = task["id"]
            summary = {"taskId": task_id, "name": task["name"], "status": task["status"]}
            async with websockets.connect(url.replace("http", "ws", 1)+f"/api/v1/tasks/{task_id}/logs") as ws:
                await ws.send(json.dumps({"token": settings.bootstrap_token}))
                sizes = []
                for _ in range(3):
                    frame = json.loads(await asyncio.wait_for(ws.recv(), 15))
                    if "data" in frame:
                        sizes.append(len(base64.b64decode(frame["data"])))
                summary["liveBytes"] = sum(sizes)
                assert summary["liveBytes"] > 0
            response = await client.post(f"/api/v1/tasks/{task_id}/commands", json={"command": "prtHardInfo"},
                                         headers={"Idempotency-Key": uuid.uuid4().hex})
            response.raise_for_status()
            command_id = response.json()["id"]
            for _ in range(40):
                command = (await client.get("/api/v1/commands/"+command_id)).json()
                if command["status"] not in ("QUEUED", "SENDING"):
                    break
                await asyncio.sleep(.5)
            summary["manualStatus"] = command["status"]
            assert command["status"] == "SENT", command
            if stop_after:
                response = await client.post(f"/api/v1/tasks/{task_id}/stop")
                response.raise_for_status()
                operation_id = response.json()["id"]
                for _ in range(120):
                    operation = (await client.get("/api/v1/operations/"+operation_id)).json()
                    if operation["status"] != "PENDING":
                        break
                    await asyncio.sleep(.5)
                assert operation["status"] == "SUCCEEDED", operation
                summary["stopStatus"] = operation["status"]
            hours = (await client.get(f"/api/v1/tasks/{task_id}/log-hours")).json()["items"]
            response = await client.post("/api/v1/downloads", json={"taskId": task_id,
                "hourIds": [h["hourId"] for h in hours], "allowPartial": False}, headers={"Idempotency-Key": uuid.uuid4().hex})
            response.raise_for_status()
            download_id = response.json()["id"]
            for _ in range(120):
                job = (await client.get("/api/v1/downloads/"+download_id)).json()
                if job["status"] not in ("QUEUED", "RUNNING"):
                    break
                await asyncio.sleep(.5)
            assert job["status"] == "SUCCEEDED", job
            response = await client.get(f"/api/v1/downloads/{download_id}/content")
            response.raise_for_status()
            summary["downloadBytes"] = len(response.content)
            part = await client.get(f"/api/v1/downloads/{download_id}/content", headers={"Range": "bytes=0-99"})
            assert part.status_code == 206 and part.content == response.content[:100]
            summary["rangeVerified"] = True
            report["tasks"].append(summary)
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--stop-after", action="store_true")
    parser.add_argument("--name-prefix", default="联调设备-")
    args = parser.parse_args()
    asyncio.run(verify(args.url, args.stop_after, args.name_prefix))
