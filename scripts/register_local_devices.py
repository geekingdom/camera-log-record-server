"""从本地受限设备清单注册联调任务，避免把凭据写入源码。"""
import argparse
import json
from pathlib import Path

import httpx
from camera_logs.common.config import Settings


def main():
    """逐台注册设备，并按设备协议逐条配置四条初始化命令。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--devices", type=Path, default=Path(".local/secrets/devices.json"))
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    config = Settings()
    with httpx.Client(base_url=args.url, headers={"Authorization": "Bearer "+config.bootstrap_token}, timeout=30) as client:
        for device in json.loads(args.devices.read_text()):
            # 设备不保证支持 shell 分号语法，因此维持四条独立且有序的初始化命令。
            device["initialCommands"] = [{"command": command, "delaySeconds": .3} for command in (
                "outputClose", "outputOpen", "setDebug -m all -l 7 -d 111", "prtHardInfo")]
            device["autoStart"] = True
            listing = client.get("/api/v1/tasks", params={"search": device["ip"], "pageSize": 100})
            listing.raise_for_status()
            existing = next((task for task in listing.json()["items"] if all(
                task.get(key) == device.get(key) for key in ("name", "ip", "port", "protocol"))), None)
            if existing:
                # 重跑联调脚本只复用同名同端点任务，不额外消耗设备有限的 SSH 连接。
                if [item["command"] for item in existing["initialCommands"]] != [item["command"] for item in device["initialCommands"]]:
                    response = client.patch("/api/v1/tasks/" + existing["id"], json={
                        "version": existing["version"], "initialCommands": device["initialCommands"]})
                    response.raise_for_status()
                print(json.dumps({"id": existing["id"], "name": existing["name"], "status": existing["status"], "reused": True}, ensure_ascii=False))
                continue
            response = client.post("/api/v1/tasks", json=device,
                headers={"Idempotency-Key": f"local-device-v3-{device['protocol']}-{device['ip']}-{device['port']}"})
            response.raise_for_status()
            task = response.json()
            print(json.dumps({"id": task["id"], "name": task["name"], "status": task["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
