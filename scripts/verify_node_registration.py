"""仅在隔离部署验收中登记真实 worker，验证 HTTP 地址与心跳、准入配置一致。"""

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx
from camera_logs.common.config import Settings


async def verify(args):
    """使用真实代理和 API 登记空闲节点；配置保留至隔离部署整体回收。"""
    settings = Settings(_env_file=args.env_file)
    async with httpx.AsyncClient(base_url=args.url.rstrip("/"), timeout=10,
                                 headers={"Authorization": "Bearer " + settings.bootstrap_token}) as client:
        response = await client.get("/api/v1/admin/nodes")
        response.raise_for_status()
        node = next(item for item in response.json()["items"] if item["id"] == args.node_id)
        assert node["online"] and not node["registered"] and node.get("activeTasks", 0) == 0
        assert node["reportedUrl"] == args.node_url
        response = await client.post("/api/v1/admin/nodes", json={
            "id": node["id"], "url": node["reportedUrl"], "capacity": node["capacity"], "accepting": True,
        })
        assert response.status_code == 201, response.status_code
        registered = response.json()
        assert registered["online"] and not registered["urlMismatch"]
        response = await client.patch("/api/v1/admin/nodes/" + args.node_id, json={
            "version": registered["version"], "capacity": node["capacity"], "accepting": True,
        })
        assert response.status_code == 200, response.status_code
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = await client.get("/api/v1/nodes")
            response.raise_for_status()
            actual = next(item for item in response.json()["items"] if item["id"] == args.node_id)
            if actual["heartbeat"] != node["reportedAt"]:
                assert actual["url"] == args.node_url
                assert actual["accepting"] and not actual.get("configurationMismatch", False)
                return {"passed": True, "httpNodeRegistered": True, "heartbeatContinued": True}
            await asyncio.sleep(.5)
        raise AssertionError("登记后未收到新的节点心跳")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--node-id", default="compose-worker-1")
    parser.add_argument("--node-url", default="http://worker:8001")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--isolated", action="store_true", required=True,
                        help="确认目标为可整体回收的隔离部署，不用于生产节点")
    print(json.dumps(asyncio.run(verify(parser.parse_args()))))
