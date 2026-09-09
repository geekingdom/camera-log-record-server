"""一键部署启动后的 Compose 服务、worker 心跳和访问地址健康检查。"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path


def _run(command: list[str], deadline: float) -> subprocess.CompletedProcess[str]:
    """在总健康检查剩余时间内运行 Docker 命令，不让单次调用无限阻塞。"""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return subprocess.CompletedProcess(command, 124, "", "deadline expired")
    try:
        return subprocess.run(command, text=True, capture_output=True, check=False, timeout=min(10, remaining))
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 124, "", "command timed out")


def _compose(project: str, compose_file: Path, env_file: Path, *args: str) -> list[str]:
    """构造显式环境文件与稳定项目名下的 Compose 调用，避免工作目录泄漏配置。"""
    return ["docker", "compose", "--env-file", str(env_file), "--project-name", project,
            "--file", str(compose_file), *args]


def _environment(service: dict, name: str, default: str) -> str:
    """从 Compose JSON 的最终环境映射取非敏感健康检查字段。"""
    values = service.get("environment", {})
    if isinstance(values, dict):
        value = values.get(name, default)
        return str(value) if value is not None else default
    if isinstance(values, list):
        prefix = name + "="
        for value in values:
            if isinstance(value, str) and value.startswith(prefix):
                return value.removeprefix(prefix)
    return default


def _frontend_url(service: dict) -> str:
    """从 Compose 解析后的端口映射返回本机可访问前端地址，不读取密钥。"""
    for port in service.get("ports", []):
        if isinstance(port, dict) and str(port.get("target")) == "80" and port.get("published") is not None:
            return f"http://127.0.0.1:{port['published']}"
    raise RuntimeError("Compose 配置缺少 frontend 对 80 端口的发布映射")


def _resolved(project: str, compose_file: Path, env_file: Path, deadline: float) -> tuple[str, str, str]:
    """读取 Compose 展开后的 JSON，避免手工解析带引号或 shell 覆盖的 `.env`。"""
    result = _run(_compose(project, compose_file, env_file, "config", "--format", "json"), deadline)
    if result.returncode:
        raise RuntimeError("无法读取 Docker Compose 最终配置")
    try:
        services = json.loads(result.stdout)["services"]
        worker, api, frontend = services["worker"], services["api"], services["frontend"]
    except (KeyError, TypeError, ValueError) as error:
        raise RuntimeError("Docker Compose 最终配置格式无效") from error
    database = _environment(api, "DATABASE_NAME", "camera_logs")
    node_id = _environment(worker, "NODE_ID", "compose-worker-1")
    return database, node_id, _frontend_url(frontend)


def _service_ready(project: str, compose_file: Path, env_file: Path, service: str, health: bool, deadline: float) -> bool:
    """检查服务容器存在且为 healthy 或 running，适配未声明健康检查的 worker。"""
    identifier = _run(_compose(project, compose_file, env_file, "ps", "-q", service), deadline).stdout.strip()
    if not identifier:
        return False
    template = "{{.State.Health.Status}}" if health else "{{.State.Status}}"
    state = _run(["docker", "inspect", "--format", template, identifier], deadline).stdout.strip()
    return state == ("healthy" if health else "running")


def _worker_heartbeat(project: str, compose_file: Path, env_file: Path, database: str, node_id: str, deadline: float) -> bool:
    """从副本集读取一分钟内的 worker 心跳，确认 API 之外的采集节点已工作。"""
    script = (
        f"const n=db.getSiblingDB({database!r}).nodes.countDocuments("
        f"{{id:{node_id!r},heartbeat:{{$gte:new Date(Date.now()-60000)}}}}); quit(n ? 0 : 1);"
    )
    command = _compose(project, compose_file, env_file, "exec", "-T", "mongo1", "mongosh", "--quiet", "--eval", script)
    return _run(command, deadline).returncode == 0


def wait_for_health(project: str, compose_file: Path, env_file: Path, timeout: float) -> str:
    """等待副本集、API、前端和 worker 心跳就绪，并返回前端可访问 URL。"""
    deadline = time.monotonic() + timeout
    database, node_id, frontend_url = _resolved(project, compose_file, env_file, deadline)
    while time.monotonic() < deadline:
        services_ok = all(
            _service_ready(project, compose_file, env_file, name, health=True, deadline=deadline)
            for name in ("mongo1", "mongo2", "mongo3", "api", "frontend")
        ) and _service_ready(project, compose_file, env_file, "worker", health=False, deadline=deadline)
        if services_ok and _worker_heartbeat(project, compose_file, env_file, database, node_id, deadline):
            return frontend_url
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise RuntimeError("部署健康检查超时：副本集、API、worker 心跳或前端尚未就绪")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=float(os.environ.get("DEPLOY_HEALTH_TIMEOUT", "180")))
    options = parser.parse_args()
    url = wait_for_health(options.project, options.compose_file, options.env_file, options.timeout)
    print(f"部署健康检查通过，前端地址：{url}")
