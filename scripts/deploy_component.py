"""独立部署的配置预检、Compose 启动和有截止时间的健康验收，仅使用标准库。"""

import argparse
import base64
import ipaddress
import json
import os
import re
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen

from deploy_health import _compose, _run, _service_ready


def require(values, *names):
    """只报告缺少的变量名称，不将密码、URI 或完整配置写入终端。"""
    for name in names:
        if not values.get(name) or "REPLACE_" in str(values[name]):
            raise ValueError(f"请在部署配置中填写 {name}，然后重新运行")


def endpoint(value, name):
    """限制代理地址为无路径和凭据的 HTTP(S) 地址，防止污染 Nginx 配置。"""
    if not re.fullmatch(r"https?://(?:[A-Za-z0-9.-]+|\[[0-9a-fA-F:]+\])(?::[0-9]{1,5})?", value):
        raise ValueError(f"{name} 必须为 http(s)://主机:端口，不能包含路径、凭据或其它文本")
    parsed = urlsplit(value)
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None  # DNS 主机名不参与 IP 通配地址判断。
    if address is not None and address.is_unspecified:
        raise ValueError(f"{name} 不能使用通配监听地址")
    try:
        port = parsed.port
        if port is not None and port < 1:
            raise ValueError()
    except ValueError as error:
        raise ValueError(f"{name} 必须使用可达主机地址及 1 到 65535 之间的端口") from error


def validate(component, config):
    """根据 Compose 展开后的最终配置验证端口、存储目录和跨服务必需信息。"""
    services = config["services"]
    service = services[{"frontend": "frontend", "backend": "api", "worker": "worker", "database": "mongo1"}[component]]
    values = service.get("environment", {})
    for item in services.values():
        for mount in item.get("volumes", []):
            if (mount.get("type") == "bind" and mount.get("target") in {"/data", "/data/db", "/var/lib/camera-logs"}
                    and (not Path(mount["source"]).is_absolute() or mount["source"] == "/")):
                raise ValueError("数据目录必须是非根目录的绝对路径")
    if component == "frontend":
        require(values, "BACKEND_UPSTREAM")
        endpoint(values["BACKEND_UPSTREAM"], "BACKEND_UPSTREAM")
    elif component in {"backend", "worker"}:
        require(values, "MONGO_URI", "ENCRYPTION_KEY", "BOOTSTRAP_TOKEN", "INTERNAL_TOKEN")
        if not values["MONGO_URI"].startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MONGO_URI 必须是 MongoDB 副本集 URI")
        try:
            if len(base64.urlsafe_b64decode(values["ENCRYPTION_KEY"])) != 32:
                raise ValueError()
        except (ValueError, TypeError) as error:
            raise ValueError("ENCRYPTION_KEY 必须是与后端一致的 Fernet 密钥") from error
        port = int(values.get("NODE_PORT" if component == "worker" else "API_PORT", "8000"))
        if not 1 <= port <= 65535:
            raise ValueError("服务端口必须在 1 到 65535 之间")
        if component == "worker":
            require(values, "NODE_ID", "NODE_URL")
            endpoint(values["NODE_URL"], "NODE_URL")
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", values["NODE_ID"]):
                raise ValueError("NODE_ID 必须使用字母数字开头的安全名称，不得包含目录分隔符")
            node_address = urlsplit(values["NODE_URL"])
            if (node_address.port or (443 if node_address.scheme == "https" else 80)) != port:
                raise ValueError("NODE_URL 的端口必须与 NODE_PORT 一致")
    else:
        require(values, "MONGO_INITDB_ROOT_USERNAME", "MONGO_INITDB_ROOT_PASSWORD")
        replica_key = services["key-init"].get("environment", {}).get("MONGO_REPLICA_KEY", "")
        if not re.fullmatch(r"[A-Za-z0-9+/=]{6,1024}", replica_key):
            raise ValueError("MONGO_REPLICA_KEY 必须为脚本生成的合法副本集认证密钥")
        ports = [int(services[f"mongo{n}"]["environment"]["MONGO_PORT"]) for n in (1, 2, 3)]
        if len(set(ports)) != 3 or any(not 1 <= port <= 65535 for port in ports):
            raise ValueError("三个 MongoDB 端口必须在合法范围内且互不相同")
        host = values["DATABASE_HOST"]
        if not re.fullmatch(r"[A-Za-z0-9.-]+", host) or host == "0.0.0.0":
            raise ValueError("DATABASE_HOST 必须为客户端可达的 IPv4 或 DNS 名称")
        sources = [next(v["source"] for v in services[f"mongo{n}"]["volumes"] if v["target"] == "/data/db")
                   for n in (1, 2, 3)]
        if len(set(sources)) != 3:
            raise ValueError("三个 MongoDB 成员必须使用不同的数据目录或卷")
    return values


def worker_ready(prefix, deadline):
    """在节点容器中同时验证本地接口和副本集心跳，不依赖本机 Python 安装项目。"""
    code = '''import asyncio, os, urllib.request
from datetime import datetime, timezone, timedelta
from pymongo import AsyncMongoClient
from camera_logs.common.config import Settings
async def verify():
    s = Settings()
    c = AsyncMongoClient(s.mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        n = await c[s.database_name].nodes.find_one({"id": s.node_id})
        assert n and n["heartbeat"] >= datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=60)
        urllib.request.urlopen("http://127.0.0.1:"+str(s.node_port)+"/health",timeout=3)
    finally:
        await c.close()
asyncio.run(verify())'''
    return _run([*prefix, "exec", "-T", "worker", "python", "-c", code], deadline).returncode == 0


def completed_successfully(prefix, service, deadline):
    """确认一次性初始化作业已存在且成功退出，避免空容器 ID 被错误传给 Docker inspect。"""
    container = _run([*prefix, "ps", "-a", "-q", service], deadline).stdout.strip()
    if not container:
        return False
    state = _run(["docker", "inspect", "--format", "{{.State.Status}} {{.State.ExitCode}}", container], deadline)
    return state.returncode == 0 and state.stdout.strip() == "exited 0"


def frontend_backend_ready(config):
    """通过真实同源代理检查后端认证路由，401/403 也是已连通的有效响应。"""
    port = next(p["published"] for p in config["services"]["frontend"]["ports"] if p["target"] == 80)
    try:
        with urlopen(f"http://127.0.0.1:{port}/api/v1/auth/me", timeout=3) as response:
            return response.status == 200
    except HTTPError as error:
        return error.code in {401, 403}
    except (OSError, URLError):
        return False


def deploy(component, project, compose_file, env_file, timeout):
    """配置错误在启动前退出；只启动选定组件，等待依赖可用且不移除其它组件。"""
    prefix = _compose(project, compose_file, env_file)
    result = _run([*prefix, "config", "--format", "json"], time.monotonic() + 30)
    if result.returncode:
        raise ValueError("Compose 配置无效或缺少必填项；请对照 deploy/config 中的中文示例检查")
    config = json.loads(result.stdout)
    values = validate(component, config)
    subprocess.run([*prefix, "up", "--build", "--detach"], check=True)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if component == "database":
            ready = all(_service_ready(project, compose_file, env_file, f"mongo{n}", True, deadline) for n in (1, 2, 3))
            jobs = ("key-init", "mongo1-user-init", "mongo2-user-init", "mongo3-user-init", "mongo-init")
            ready = ready and all(completed_successfully(prefix, job, deadline) for job in jobs)
        elif component == "worker":
            ready = _service_ready(project, compose_file, env_file, "worker", False, deadline) and worker_ready(prefix, deadline)
        else:
            service = "api" if component == "backend" else "frontend"
            ready = _service_ready(project, compose_file, env_file, service, True, deadline)
            if component == "frontend" and ready:
                ready = frontend_backend_ready(config)
        if ready:
            print(f"{component} 部署健康检查通过；容器已设置服务器重启后自动恢复")
            if component == "worker":
                print(f"采集节点：{values['NODE_ID']}；请在后台节点配置中核对公布地址与容量")
            return
        time.sleep(min(2, max(0, deadline-time.monotonic())))
    raise RuntimeError(f"{component} 健康检查超时；请检查该组件容器日志和跨主机网络")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=("frontend", "backend", "worker", "database"), required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--compose-file", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=float(os.getenv("DEPLOY_HEALTH_TIMEOUT", "240")))
    args = parser.parse_args()
    try:
        deploy(args.component, args.project, args.compose_file, args.env_file, args.timeout)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        # CalledProcessError 的命令不含凭据；不输出 Compose 原始配置或数据库连接字符串。
        import sys
        print(f"部署未完成：{error}；已有数据未删除", file=sys.stderr)
        raise SystemExit(1)
