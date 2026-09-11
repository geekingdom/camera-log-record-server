"""仅在隔离部署验收中登记真实 worker，验证 HTTP 地址与心跳、准入配置一致。"""

import argparse
import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import httpx
from camera_logs.common.config import Settings


def _run(command: list[str], deadline: float) -> subprocess.CompletedProcess[str]:
    """在总超时内执行 Compose 配置读取，避免验收命令无限等待。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return subprocess.CompletedProcess(command, 124, "", "deadline expired")
    try:
        return subprocess.run(
            command, text=True, capture_output=True, check=False, timeout=min(10, remaining)
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", "command timed out")
    except OSError:
        return subprocess.CompletedProcess(command, 127, "", "command unavailable")


def _compose(args) -> list[str]:
    """构造显式 Compose 配置调用，不读取或回显完整部署环境。"""
    return [
        "docker",
        "compose",
        "--env-file",
        str(args.env_file),
        "--project-name",
        args.project,
        "--file",
        str(args.compose_file),
    ]


def _environment(service: dict, name: str) -> str | None:
    """从展开配置的 worker 环境中读取登记所需的非敏感字段。"""
    values = service.get("environment", {})
    if isinstance(values, dict):
        value = values.get(name)
        return str(value) if value is not None else None
    if isinstance(values, list):
        prefix = name + "="
        for value in values:
            if isinstance(value, str) and value.startswith(prefix):
                return value.removeprefix(prefix)
    return None


def _resolved(args, deadline: float) -> tuple[str, str]:
    """由 Compose 最终 worker 配置取得节点 ID 和公布地址，不从 API 自证。"""
    result = _run([*_compose(args), "config", "--format", "json"], deadline)
    if result.returncode:
        raise RuntimeError("无法读取 Docker Compose 最终配置")
    try:
        worker = json.loads(result.stdout)["services"]["worker"]
        if not isinstance(worker, dict):
            raise TypeError("worker service is not an object")
        node_id = _environment(worker, "NODE_ID")
        node_url = _environment(worker, "NODE_URL")
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Docker Compose 最终配置格式无效") from error
    if not node_id or not node_url:
        raise RuntimeError("Docker Compose worker 缺少 NODE_ID 或 NODE_URL")
    return node_id, node_url


def _node_identity(args, deadline: float) -> tuple[str, str]:
    """显式参数优先；缺失部分才由部署最终配置补齐。"""
    if args.node_id is not None and args.node_url is not None:
        return args.node_id, args.node_url
    configured_id, configured_url = _resolved(args, deadline)
    return args.node_id or configured_id, args.node_url or configured_url


async def verify(args):
    """使用真实代理和 API 登记空闲节点；配置保留至隔离部署整体回收。"""
    settings = Settings(_env_file=args.env_file)
    node_id, node_url = _node_identity(args, time.monotonic() + args.timeout)
    async with httpx.AsyncClient(
        base_url=args.url.rstrip("/"),
        timeout=10,
        headers={"Authorization": "Bearer " + settings.bootstrap_token},
    ) as client:
        response = await client.get("/api/v1/admin/nodes")
        response.raise_for_status()
        node = next(item for item in response.json()["items"] if item["id"] == node_id)
        assert node["online"] and not node["registered"] and node.get("activeTasks", 0) == 0
        assert node["reportedUrl"] == node_url
        response = await client.post(
            "/api/v1/admin/nodes",
            json={
                "id": node["id"],
                "url": node["reportedUrl"],
                "capacity": node["capacity"],
                "accepting": True,
            },
        )
        assert response.status_code == 201, response.status_code
        registered = response.json()
        assert registered["online"] and not registered["urlMismatch"]
        response = await client.patch(
            "/api/v1/admin/nodes/" + node_id,
            json={
                "version": registered["version"],
                "capacity": node["capacity"],
                "accepting": True,
            },
        )
        assert response.status_code == 200, response.status_code
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            response = await client.get("/api/v1/nodes")
            response.raise_for_status()
            actual = next(item for item in response.json()["items"] if item["id"] == node_id)
            if actual["heartbeat"] != node["reportedAt"]:
                assert actual["url"] == node_url
                assert actual["accepting"] and not actual.get("configurationMismatch", False)
                return {"passed": True, "httpNodeRegistered": True, "heartbeatContinued": True}
            await asyncio.sleep(0.5)
        raise AssertionError("登记后未收到新的节点心跳")


def parse_arguments(arguments=None):
    """解析隔离部署登记参数，默认值与 Compose 验收项目保持一致。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--node-id")
    parser.add_argument("--node-url")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--project", default=os.environ.get("COMPOSE_PROJECT_NAME", "camera-logs"))
    parser.add_argument("--compose-file", type=Path, default=Path("deploy/docker-compose.yml"))
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument(
        "--isolated",
        action="store_true",
        required=True,
        help="确认目标为可整体回收的隔离部署，不用于生产节点",
    )
    return parser.parse_args(arguments)


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify(parse_arguments()))))
