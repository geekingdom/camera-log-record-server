"""在 Linux Docker 主机验证数据库、后端、节点和前端可分开重复部署。"""

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import urlopen
from uuid import uuid4

from component_diagnostics import diagnose_process_failure

ROOT = Path(__file__).resolve().parents[1]
COMPONENTS = ("database", "backend", "worker", "frontend")


def check(condition, message):
    """以不含配置值的中文错误标识验证失败，避免在 CI 日志泄露临时凭据。"""
    if not condition:
        raise AssertionError(message)


def run(command, *, timeout=300, environment=None, stage="Docker 命令"):
    """运行部署命令，失败时只输出阶段和退出码，避免回显环境文件中的凭据。"""
    result = subprocess.run(command, cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
                            timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{stage}失败，退出码 {result.returncode}")
    return result


def write_environment(path, values):
    """写入验证专用 0600 环境文件，值只存在临时目录且不会打印。"""
    path.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8")
    path.chmod(0o600)


def bridge_gateway():
    """获取 Linux 默认 bridge 网关，供前端容器代理 host-network API。"""
    result = run(["docker", "network", "inspect", "bridge", "--format", "{{(index .IPAM.Config 0).Gateway}}"])
    gateway = result.stdout.strip()
    check(gateway.count(".") == 3, "无法取得 Linux Docker bridge 网关")
    return gateway


def fernet_key():
    """生成后端与 Worker 共享的临时 Fernet 密钥。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def compose(project, component, env_file, *args):
    """构造受随机项目名和对应组件 YAML 限定的 Compose 命令。"""
    return ["docker", "compose", "--env-file", str(env_file), "--project-name", project,
            "--file", str(ROOT / "deploy" / f"{component}.yml"), *args]


def compose_environment(env_file):
    """让 Compose 变量替换和 YAML 的 env_file 始终读取同一份临时配置。"""
    return os.environ | {"DEPLOY_ENV_FILE": str(env_file)}


def component_states(project):
    """返回随机项目容器的受限状态，用于失败定位且不读取日志或环境变量。"""
    result = subprocess.run([
        "docker", "ps", "--all", "--filter", f"label=com.docker.compose.project={project}",
        "--format", "{{.Names}} {{.State}}",
    ], cwd=ROOT, text=True, capture_output=True, check=False, timeout=30)
    if result.returncode:
        return "状态不可读取"
    states = "; ".join(line for line in result.stdout.splitlines() if line)
    return states or "未发现容器"


def http_ready(url, *, allow_unauthorized=False, deadline):
    """在截止时间内轮询 HTTP 服务；前端代理的 401/403 表示后端已经可达。"""
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=3) as response:
                if 200 <= response.status < 300:
                    return True
        except HTTPError as error:
            if allow_unauthorized and error.code in {401, 403}:
                return True
        except (OSError, URLError):
            pass
        time.sleep(2)
    return False


def worker_ready(project, env_file, *, deadline):
    """确认重启后的节点健康接口和副本集心跳均恢复，不打印其数据库连接配置。"""
    code = '''import asyncio, urllib.request
from datetime import datetime, timezone, timedelta
from pymongo import AsyncMongoClient
from camera_logs.common.config import Settings
async def verify():
    settings = Settings()
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=3000)
    try:
        node = await client[settings.database_name].nodes.find_one({"id": settings.node_id})
        assert node and node["heartbeat"] >= datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=60)
        urllib.request.urlopen("http://127.0.0.1:"+str(settings.node_port)+"/health", timeout=3)
    finally:
        await client.close()
asyncio.run(verify())'''
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            result = subprocess.run(compose(project, "worker", env_file, "exec", "-T", "worker", "python", "-c", code),
                                    cwd=ROOT, env=compose_environment(env_file), text=True, capture_output=True,
                                    check=False, timeout=min(10, remaining))
            if result.returncode == 0:
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    return False


def deploy_component(component, project, env_file, timeout, *, secret_values=()):
    """通过正式独立入口启动单一组件，并使用私有环境文件和随机项目名。"""
    command = ["bash", str(ROOT / f"deploy-{component}.sh"), "--env-file", str(env_file)]
    environment = compose_environment(env_file) | {
        "COMPOSE_PROJECT_NAME": project,
        "DEPLOY_HEALTH_TIMEOUT": str(timeout),
    }
    result = subprocess.run(command, cwd=ROOT, env=environment, text=True, capture_output=True, check=False,
                            timeout=timeout + 60)
    if result.returncode:
        resolved_project = f"{project}-{component}"
        diagnostic = diagnose_process_failure(f"{component} 独立部署", result.returncode,
                                              result.stdout, result.stderr, secrets=secret_values)
        raise RuntimeError(f"{diagnostic}；容器状态：{component_states(resolved_project)}")


def restart_component(project, component, env_file):
    """重启常驻容器以确认 restart 策略后的端口、代理和数据库连接仍可恢复。"""
    services = {"database": ("mongo1", "mongo2", "mongo3"), "backend": ("api",),
                "worker": ("worker",), "frontend": ("frontend",)}[component]
    try:
        run(compose(project, component, env_file, "restart", *services), timeout=120,
            environment=compose_environment(env_file), stage=f"{component} 重启")
    except RuntimeError as error:
        raise RuntimeError(f"{error}；容器状态：{component_states(project)}") from error


def _temporary_root(data_root):
    """限制清理器只处理本验证由 tempfile 创建的目录，防止路径误用扩大删除范围。"""
    root, parent = data_root.resolve(), Path(tempfile.gettempdir()).resolve()
    try:
        root.relative_to(parent)
    except ValueError as error:
        raise RuntimeError("验证临时目录不在系统临时目录中，拒绝清理") from error
    if not root.name.startswith("camera-component-deploy-"):
        raise RuntimeError("验证临时目录名称不匹配，拒绝清理")
    return root


def cleanup(projects, environments, data_root):
    """删除随机项目并验证无容器、卷和临时挂载目录残留；失败会保留阶段证据。"""
    failures = []
    for component, project in projects.items():
        environment = compose_environment(environments[component])
        try:
            run(compose(project, component, environments[component], "down", "--volumes", "--remove-orphans"),
                timeout=120, environment=environment, stage=f"{component} 清理")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
            failures.append(str(error))
        for noun, command in (
            ("容器", ["docker", "ps", "--all", "--quiet", "--filter", f"label=com.docker.compose.project={project}"]),
            ("卷", ["docker", "volume", "ls", "--quiet", "--filter", f"label=com.docker.compose.project={project}"]),
        ):
            try:
                result = run(command, timeout=30, stage=f"{component} 清理后检查{noun}")
                if result.stdout.strip():
                    failures.append(f"{component} 清理后仍存在{noun}")
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
                failures.append(str(error))
    root = _temporary_root(data_root)
    try:
        # Mongo 容器可能以 root 创建挂载内容；仅将本次临时根挂入短生命周期容器清空后再由宿主移除。
        run(["docker", "run", "--rm", "--mount", f"type=bind,source={root},target=/verification",
             "alpine:3.21", "sh", "-ec", "find /verification -mindepth 1 -delete"], timeout=120,
            stage="临时挂载目录清理")
        shutil.rmtree(root)
        if root.exists():
            failures.append("临时挂载目录仍存在")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        failures.append(str(error))
    if failures:
        raise RuntimeError("；".join(failures))


def verify(timeout):
    """以同一随机副本集配置完成独立启动、重跑、重启与 HTTP 可达性验证。"""
    check(sys.platform.startswith("linux"), "独立组件真实部署验证只能在 Linux 运行")
    run(["docker", "compose", "version"], timeout=30)
    identity, data_root = uuid4().hex, Path(tempfile.mkdtemp(prefix="camera-component-deploy-"))
    project_base = f"camera-components-{identity}"
    projects = {name: f"{project_base}-{name}" for name in COMPONENTS}
    environments = {name: data_root / f".{name}.env" for name in COMPONENTS}
    password, replica_key = secrets.token_urlsafe(30), base64.b64encode(secrets.token_bytes(384)).decode("ascii")
    encryption, bootstrap, internal = fernet_key(), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    failure = None
    try:
        gateway = bridge_gateway()
        uri = (f"mongodb://camera_admin:{quote(password, safe='')}@127.0.0.1:27117,127.0.0.1:27118,"
               "127.0.0.1:27119/camera_logs?replicaSet=rs0&authSource=admin")
        values = {
            "database": {"DATABASE_HOST": "127.0.0.1", "DATABASE_BIND_IP": "0.0.0.0", "MONGO_PORT_1": "27117",
                         "MONGO_PORT_2": "27118", "MONGO_PORT_3": "27119", "MONGO_ROOT_USERNAME": "camera_admin",
                         "MONGO_ROOT_PASSWORD": password, "MONGO_REPLICA_KEY": replica_key,
                         "MONGO_DATA_1": str(data_root / "mongo1"), "MONGO_DATA_2": str(data_root / "mongo2"),
                         "MONGO_DATA_3": str(data_root / "mongo3")},
            "backend": {"MONGO_URI": uri, "DATABASE_NAME": "camera_logs", "ENCRYPTION_KEY": encryption,
                        "BOOTSTRAP_TOKEN": bootstrap, "INTERNAL_TOKEN": internal, "API_BIND_IP": "0.0.0.0",
                        "API_PORT": "18000", "FORWARDED_ALLOW_IPS": gateway, "API_DATA_ROOT": str(data_root / "api"),
                        "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "asdf!234", "RETENTION_DAYS": "7"},
            "worker": {"MONGO_URI": uri, "DATABASE_NAME": "camera_logs", "ENCRYPTION_KEY": encryption,
                       "BOOTSTRAP_TOKEN": bootstrap, "INTERNAL_TOKEN": internal, "NODE_ID": f"component-{identity[:12]}",
                       "NODE_URL": "http://127.0.0.1:18001", "NODE_PORT": "18001", "NODE_BIND_IP": "0.0.0.0",
                       "HOST_LOG_ROOT": str(data_root / "worker"), "RETENTION_DAYS": "7"},
            "frontend": {"BACKEND_UPSTREAM": f"http://{gateway}:18000", "FRONTEND_PORT": "15174"},
        }
        for name in COMPONENTS:
            write_environment(environments[name], values[name])
        # 包括本轮所有组件及继承环境的凭据，防止跨组件错误回显同一副本集口令。
        sensitive_keys = ("PASSWORD", "TOKEN", "SECRET", "KEY", "URI", "USERNAME")
        secret_values = tuple(value for mapping in (os.environ, *values.values()) for key, value in mapping.items()
                              if value and any(marker in key.upper() for marker in sensitive_keys))
        before = {name: environments[name].read_bytes() for name in COMPONENTS}
        for name in COMPONENTS:
            deploy_component(name, project_base, environments[name], timeout, secret_values=secret_values)
        deadline = time.monotonic() + timeout
        check(http_ready("http://127.0.0.1:18000/health", deadline=deadline), "后端健康接口未恢复")
        check(http_ready("http://127.0.0.1:15174/api/v1/auth/me", allow_unauthorized=True, deadline=deadline),
              "前端未能经 Linux host 网关代理后端")
        for name in COMPONENTS:
            deploy_component(name, project_base, environments[name], timeout, secret_values=secret_values)
        check(all(environments[name].read_bytes() == before[name] for name in COMPONENTS), "重复部署改写了环境配置")
        for name in COMPONENTS:
            restart_component(projects[name], name, environments[name])
        deadline = time.monotonic() + timeout
        check(http_ready("http://127.0.0.1:18000/health", deadline=deadline), "重启后端后健康接口未恢复")
        check(http_ready("http://127.0.0.1:15174/api/v1/auth/me", allow_unauthorized=True, deadline=deadline),
              "重启后前端代理未恢复")
        check(worker_ready(projects["worker"], environments["worker"], deadline=deadline),
              "重启后采集节点健康接口或副本集心跳未恢复")
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            cleanup(projects, environments, data_root)
        except RuntimeError as cleanup_error:
            if failure is not None:
                raise RuntimeError(f"验证失败：{failure}；清理失败：{cleanup_error}") from failure
            raise
    return {"passed": True, "linux": True, "independentComponents": True, "repeatPreservesEnvironment": True,
            "restartHealth": True, "temporaryProjectsRemoved": True, "temporaryDataRemoved": True}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=240)
    arguments = parser.parse_args()
    try:
        print(json.dumps(verify(arguments.timeout)))
    except (AssertionError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"独立组件部署验证失败：{error}", file=sys.stderr)
        raise SystemExit(1)
