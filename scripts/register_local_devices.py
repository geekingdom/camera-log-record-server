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
    devices = json.loads(args.devices.read_text())
    for device in devices:
        if not device.get("resourceId"):
            raise ValueError(f"设备 {device.get('name', device['ip'])} 缺少 resourceId；请先添加资源或在设备清单填写 resourceId")
    with httpx.Client(base_url=args.url, headers={"Authorization": "Bearer "+config.bootstrap_token}, timeout=30) as client:
        for index, device in enumerate(devices):
            # 设备不保证支持 shell 分号语法，因此维持四条独立且有序的初始化命令。
            device["initialCommands"] = [{"command": command, "delaySeconds": .3} for command in (
                "outputClose", "outputOpen", "setDebug -m all -l 7 -d 111", "prtHardInfo")]
            device["autoStart"] = True
            response = client.post("/api/v1/tasks", json=device,
                headers={"Idempotency-Key": f"local-device-v4-{index}"})
            response.raise_for_status()
            task = response.json()
            print(json.dumps({"id": task["id"], "name": task["name"], "status": task["status"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
