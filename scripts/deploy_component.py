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

_WORKER_STOP_TIMEOUT_SECONDS = 150
_MONGO_URI = re.compile(r"mongodb(?:\+srv)?://\S+", re.IGNORECASE)
_SENSITIVE_DIAGNOSTIC_VALUE = re.compile(
    r"(?i)(?P<prefix>[\"']?(?:[A-Za-z0-9_.-]*(?:password|secret|token|key)[A-Za-z0-9_.-]*)[\"']?\s*[=:]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,}\]]+)"
)


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
        default_port = "18081" if component == "worker" else "18080"
        port = int(values.get("NODE_PORT" if component == "worker" else "API_PORT", default_port))
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


def _recovery_result(prefix, phase, deadline, *, token=None):
    """用新 Worker 镜像执行一次性恢复阶段，并只接受模块输出的单行 JSON。"""
    command = [*prefix, "run", "--rm", "--no-deps", "worker", "python", "-m",
               "camera_logs.node.deployment_recovery", phase]
    if token is not None:
        command.extend(["--token", token])
    result = _run(command, deadline, command_timeout=max(1, deadline - time.monotonic()))
    if result.returncode:
        raise RuntimeError(f"Worker 部署恢复{phase}阶段失败 exit={result.returncode}；{_diagnostic(result)}")
    for line in reversed(result.stdout.splitlines()):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise RuntimeError(f"Worker 部署恢复{phase}阶段未返回有效证明")


def _diagnostic(result):
    """返回部署子进程最后的脱敏诊断，避免 prepare 失败时仅剩抽象错误。"""
    output = "\n".join(part for part in (result.stdout, result.stderr) if part).strip()
    output = _MONGO_URI.sub("<redacted-mongo-uri>", output)
    output = _SENSITIVE_DIAGNOSTIC_VALUE.sub(r"\g<prefix><redacted>", output)
    return output[-1200:] or "无安全诊断输出"


def _phase_deadline(timeout, *, minimum=0):
    """每个部署阶段独立计时，构建或 prepare 不得侵占 Worker 停止宽限期。"""
    return time.monotonic() + max(timeout, minimum)


def _stopped_container(container, deadline):
    """轮询 Docker inspect 的真实运行标志，不能用 Compose 心跳代替旧容器已停止证明。"""
    while time.monotonic() < deadline:
        result = _run(["docker", "inspect", "--format", "{{.State.Running}}", container], deadline)
        if result.returncode:
            raise RuntimeError("无法确认旧 Worker 容器状态")
        if result.stdout.strip() == "false":
            return
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    raise RuntimeError("旧 Worker 容器停止确认超时，未释放任何任务锁")


def _recovery_journal(path, payload=None):
    """原子保存主机侧停机事实，重试不能从数据库标记猜测容器是否已经停止。"""
    if payload is None:
        if not path.exists():
            return None
        return json.loads(path.read_text())
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True))
    temporary.replace(path)
    return payload


def deploy_worker_with_recovery(prefix, timeout, *, build=True, journal=None):
    """按准备、停止确认、完成的固定顺序替换 Worker，未知容器状态绝不完成恢复。"""
    if build:
        built_deadline = _phase_deadline(timeout)
        built = _run([*prefix, "build", "worker"], built_deadline,
                     command_timeout=max(1, built_deadline - time.monotonic()))
        if built.returncode:
            raise RuntimeError(f"Worker 新镜像构建失败 exit={built.returncode}；{_diagnostic(built)}")
    current = _run([*prefix, "ps", "-q", "worker"], _phase_deadline(timeout, minimum=30))
    if current.returncode:
        raise RuntimeError("无法读取现有 Worker 容器，未开始部署恢复")
    old = current.stdout.strip()
    if not old:
        pending = _recovery_journal(journal) if journal else None
        if pending:
            if pending.get("compose") != prefix:
                raise RuntimeError("Worker 部署恢复记录不属于当前 Compose 项目或环境")
            _stopped_container(pending["container"], _phase_deadline(timeout, minimum=_WORKER_STOP_TIMEOUT_SECONDS))
            completed = _recovery_result(prefix, "complete", _phase_deadline(timeout), token=pending["token"])
            journal.unlink()
            started_deadline = _phase_deadline(timeout, minimum=30)
            started = _run([*prefix, "up", "--detach", "worker"], started_deadline,
                           command_timeout=max(1, started_deadline - time.monotonic()))
            if started.returncode:
                raise RuntimeError("Worker 恢复完成后启动失败")
            return completed
        # 首次部署没有本次脚本可停止的旧容器，不能把遗留 BLOCKED 任务当成关闭证据。
        started_deadline = _phase_deadline(timeout, minimum=30)
        started = _run([*prefix, "up", "--detach", "worker"], started_deadline,
                       command_timeout=max(1, started_deadline - time.monotonic()))
        if started.returncode:
            raise RuntimeError("Worker 首次启动失败")
        return {"prepared": 0, "proved": 0, "released": 0, "reason": "NO_RUNNING_OLD_WORKER"}
    prepared = _recovery_result(prefix, "prepare", _phase_deadline(timeout))
    token = prepared.get("token")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        raise RuntimeError("Worker 部署恢复准备阶段未返回合法令牌")
    unsafe = ("incompleteOwner", "missingRun", "runSessionConflict", "ownerChanged")
    if any(prepared.get(reason, 0) for reason in unsafe):
        raise RuntimeError("Worker 部署恢复准备阶段存在无法证明的旧运行，未停止容器")
    if journal:
        _recovery_journal(journal, {"token": token, "container": old, "compose": prefix})
    stopped_deadline = _phase_deadline(timeout, minimum=_WORKER_STOP_TIMEOUT_SECONDS)
    stopped = _run([*prefix, "stop", "worker"], stopped_deadline,
                   command_timeout=_WORKER_STOP_TIMEOUT_SECONDS)
    if stopped.returncode:
        raise RuntimeError(f"旧 Worker 容器停止失败 exit={stopped.returncode}；{_diagnostic(stopped)}")
    _stopped_container(old, _phase_deadline(timeout, minimum=_WORKER_STOP_TIMEOUT_SECONDS))
    completed = _recovery_result(prefix, "complete", _phase_deadline(timeout), token=token)
    if journal:
        journal.unlink(missing_ok=True)
    started_deadline = _phase_deadline(timeout, minimum=30)
    started = _run([*prefix, "up", "--detach", "worker"], started_deadline,
                   command_timeout=max(1, started_deadline - time.monotonic()))
    if started.returncode:
        raise RuntimeError("Worker 新容器启动失败；已写入的关闭证明保留供后续恢复")
    return completed


def deploy(component, project, compose_file, env_file, timeout, *, build=True):
    """配置错误在启动前退出；只启动选定组件，等待依赖可用且不移除其它组件。"""
    prefix = _compose(project, compose_file, env_file)
    result = _run([*prefix, "config", "--format", "json"], time.monotonic() + 30)
    if result.returncode:
        raise ValueError("Compose 配置无效或缺少必填项；请对照 deploy/config 中的中文示例检查")
    config = json.loads(result.stdout)
    values = validate(component, config)
    if component == "worker":
        journal = Path(env_file).resolve().parent / f".worker-deployment-recovery-{project}.json"
        recovery = deploy_worker_with_recovery(prefix, timeout, build=build, journal=journal)
        print(f"Worker 部署恢复结果：{json.dumps(recovery, ensure_ascii=False, sort_keys=True)}")
    else:
        command = [*prefix, "up", "--detach"]
        if build:
            command.insert(-1, "--build")
        subprocess.run(command, check=True)
    # 构建与受控关闭有独立阶段预算；健康检查必须从新容器启动后重新计时。
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
    parser.add_argument("--no-build", action="store_true", help="镜像已由完整部署阶段构建，不重复构建")
    args = parser.parse_args()
    try:
        deploy(args.component, args.project, args.compose_file, args.env_file, args.timeout, build=not args.no_build)
    except (ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        # CalledProcessError 的命令不含凭据；不输出 Compose 原始配置或数据库连接字符串。
        import sys
        print(f"部署未完成：{error}；已有数据未删除", file=sys.stderr)
        raise SystemExit(1)
