"""无 Docker 的 Linux 主机部署编排：配置预检、隔离依赖、systemd 与健康检查。"""

import argparse
import hashlib
import json
import os
import pwd
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from configure_nfs_export import configure as configure_nfs_export
from native_config import create_config, read_config, validate
from native_packages import build_frontend, install_packages, prepare_venv, python_runtime, run
from native_units import render

COMPONENTS = ("database", "backend", "worker", "frontend")
SERVICES = {"database": "mongo", "backend": "api", "worker": "worker", "frontend": "frontend"}
CONTRACT_KEYS = ("ENCRYPTION_KEY", "INTERNAL_TOKEN", "BOOTSTRAP_TOKEN", "MONGO_URI", "MONGO_PORT",
                 "MONGO_BIND_IP", "MONGO_ADVERTISED_HOST", "API_PORT", "API_BIND_IP", "BACKEND_UPSTREAM",
                 "NODE_PORT", "NODE_URL", "NODE_BIND_IP", "NODE_ID", "DATABASE_NAME")


def contract_hash(values):
    """记录跨组件合同摘要，防止单独更新使仍运行的另一组件失联。"""
    return hashlib.sha256(json.dumps({key: values[key] for key in CONTRACT_KEYS}, sort_keys=True).encode()).hexdigest()


def atomic_write(path, content, mode=0o600):
    """同目录替换配置，避免systemd或数据库读取半份文件。"""
    path = Path(path)
    if path.is_symlink():
        raise ValueError("受管配置不能是符号链接")
    temporary = path.with_suffix(path.suffix + ".new")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def preflight(values, components):
    """拒绝覆盖未知安装或占用端口；现有受管实例可重复部署。"""
    root = Path(values["INSTALL_ROOT"])
    marker = root / ".native-managed.json"
    if root.exists() and any(root.iterdir()) and not marker.exists():
        raise ValueError("INSTALL_ROOT非空且非受管安装，拒绝覆盖")
    if marker.exists():
        known = json.loads(marker.read_text())
        for key in ("SERVICE_USER", "DATA_ROOT", "MONGO_DATA_ROOT", "LOG_ROOT", "API_LOG_ROOT"):
            if values[key] != known.get(key):
                raise ValueError(f"{key}已改变，请先按文档停止服务和迁移数据，不自动搬迁")
        if known.get("encryptionKeyHash") != hashlib.sha256(values["ENCRYPTION_KEY"].encode()).hexdigest():
            raise ValueError("ENCRYPTION_KEY与受管安装不一致，拒绝覆盖已有设备密码加密密钥")
        if known.get("contractHash") != contract_hash(values):
            raise ValueError("跨组件连接或凭据配置已变化，请按协调迁移流程处理，拒绝先停止现有服务")
    for component in components:
        service = f"camera-logs-{SERVICES[component]}.service"
        unit = Path("/etc/systemd/system") / service
        if unit.exists():
            if not marker.exists() or str(root) not in unit.read_text():
                raise ValueError(f"{service}属于其它安装，拒绝覆盖")
            continue
        key = {"database": "MONGO_PORT", "backend": "API_PORT", "worker": "NODE_PORT", "frontend": "FRONTEND_PORT"}[component]
        with socket.socket() as probe:
            try:
                probe.bind(("0.0.0.0", int(values[key])))
            except OSError as error:
                raise ValueError(f"{key}端口已被其它服务占用，请先修改配置") from error
    if "database" in components:
        data = Path(values["MONGO_DATA_ROOT"])
        if data.exists() and any(data.iterdir()) and not marker.exists():
            raise ValueError("MongoDB目录存在未知数据，拒绝初始化")


def prepare_directories(values):
    """只初始化明确目录权限，不递归改写既有设备日志的权限。"""
    user = values["SERVICE_USER"]
    try:
        account = pwd.getpwnam(user)
        if account.pw_uid == 0 or account.pw_shell not in {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false"}:
            raise ValueError("SERVICE_USER已有交互账号，请使用专用系统账号")
    except KeyError:
        run(["useradd", "--system", "--user-group", "--no-create-home", "--shell", "/usr/sbin/nologin", user])
        account = pwd.getpwnam(user)
    root = Path(values["INSTALL_ROOT"])
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o755)
    paths = [root / "etc", *(Path(values[key]) for key in ("DATA_ROOT", "LOG_ROOT", "API_LOG_ROOT", "MONGO_DATA_ROOT", "NFS_ROOT")),
             Path(values["DATA_ROOT"]) / "nginx"]
    for directory in paths:
        if directory.is_symlink():
            raise ValueError("受管数据目录不能是符号链接")
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, account.pw_uid, account.pw_gid)
        directory.chmod(0o750)
    marker = root / ".native-managed.json"
    if not marker.exists():
        manifest = {key: values[key] for key in (
            "SERVICE_USER", "DATA_ROOT", "MONGO_DATA_ROOT", "LOG_ROOT", "API_LOG_ROOT")}
        manifest["encryptionKeyHash"] = hashlib.sha256(values["ENCRYPTION_KEY"].encode()).hexdigest()
        manifest["contractHash"] = contract_hash(values)
        atomic_write(marker, json.dumps(manifest), 0o600)
    return account


def publish(values, component, files, account):
    """只发布选定组件的配置；成员key由Mongo运行账号独占读取。"""
    root = Path(values["INSTALL_ROOT"])
    for name, content in files.items():
        if name.endswith(".service"):
            path = Path("/etc/systemd/system") / f"camera-logs-{name}"
            atomic_write(path, content, 0o644)
        else:
            path = root / "etc" / name
            if name == "mongo.key" and path.exists() and path.read_text() != content:
                raise ValueError("已有Mongo成员密钥与配置不同，拒绝自动替换")
            atomic_write(path, content, 0o400 if name == "mongo.key" else 0o600)
            os.chown(path, account.pw_uid, account.pw_gid)
    if component == "frontend":
        # nginx -t也会创建PID文件，必须与实际服务使用同一账号，否则首次启动Permission denied。
        pid = Path(values["DATA_ROOT"]) / "nginx/nginx.pid"
        if pid.exists() and not pid.is_symlink():
            os.chown(pid, account.pw_uid, account.pw_gid)
        run(["runuser", "-u", values["SERVICE_USER"], "--", "/usr/sbin/nginx", "-t", "-c", root / "etc/nginx.conf"])
    run(["systemctl", "daemon-reload"])
    service = f"camera-logs-{SERVICES[component]}"
    run(["systemctl", "enable", service])
    run(["systemctl", "restart", service])


def wait_http(url, *, allow_auth=False, timeout=90):
    """等待真实HTTP响应，不能仅以systemd active冒充服务就绪。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=3) as response:
                if response.status == 200:
                    return
        except HTTPError as error:
            if allow_auth and error.code in {401, 403}:
                return
        except (URLError, OSError):
            pass
        time.sleep(1)
    raise RuntimeError("服务HTTP健康检查超时，请查看对应systemd日志")


def deploy(values, component, config, source):
    """按数据库、API、节点、前端顺序安装；更新一个组件不停止其它组件。"""
    components = COMPONENTS if component == "all" else (component,)
    preflight(values, components)
    if values["INSTALL_PACKAGES"] == "true":
        install_packages(component)
    account = prepare_directories(values)
    if component in {"all", "worker"}:
        configure_nfs_export(
            values,
            install_package=values["INSTALL_PACKAGES"] == "true",
            owner=(account.pw_uid, account.pw_gid),
        )
    python = python_runtime(values) if component != "frontend" else None
    files = render(values, source)
    for selected in components:
        service = f"camera-logs-{SERVICES[selected]}"
        database_pending = Path(values["INSTALL_ROOT"]) / "etc/mongo-initializing"
        if selected == "database" and not any(Path(values["MONGO_DATA_ROOT"]).iterdir()):
            # 在mongod首次写数据前记录来源；中断后只允许相同配置继续创建首个账号。
            fingerprint = hashlib.sha256(config.read_bytes()).hexdigest()
            if not database_pending.exists():
                atomic_write(database_pending, fingerprint)
        if Path(f"/etc/systemd/system/{service}.service").exists():
            # 先正常关闭选定服务再更新其依赖；worker停止会排空日志，由既有运行恢复规则处理。
            run(["systemctl", "stop", service])
        print(f"正在部署原生组件：{selected}", flush=True)
        if selected == "frontend":
            build_frontend(values, source)
        else:
            executable = prepare_venv(values, source, selected, python)
        publish(values, selected, files[selected], account)
        if selected == "database":
            # auth从第一次启动即启用，初始化器借助仅本机可用的localhost exception建立账号。
            options = []
            if database_pending.exists():
                if database_pending.read_text() != hashlib.sha256(config.read_bytes()).hexdigest():
                    raise ValueError("数据库首次初始化中断后配置改变，请恢复原配置再重试")
                options.append("--allow-initialize")
            run([executable, source / "scripts/native_database.py", "--config", config, *options])
            database_pending.unlink(missing_ok=True)
        elif selected in {"backend", "worker"}:
            key = "API_PORT" if selected == "backend" else "NODE_PORT"
            bind = values["API_BIND_IP" if selected == "backend" else "NODE_BIND_IP"]
            host = "127.0.0.1" if bind == "0.0.0.0" else f"[{bind}]" if ":" in bind else bind
            wait_http(f"http://{host}:{values[key]}/health")
            if selected == "worker":
                run([executable, source / "scripts/native_health.py", "--config", config])
        else:
            wait_http(f"http://127.0.0.1:{values['FRONTEND_PORT']}/")
            wait_http(f"http://127.0.0.1:{values['FRONTEND_PORT']}/api/v1/auth/me", allow_auth=True)
    print("原生部署健康检查通过；systemd服务已设置开机启动。", flush=True)
    if "frontend" in components:
        print(f"平台访问地址：http://服务器IP:{values['FRONTEND_PORT']}")


def main(argv=None):
    """默认完整部署；--init仅创建可编辑配置，不安装软件或操作现有服务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("component", nargs="?", choices=("all", *COMPONENTS), default="all")
    parser.add_argument("--config", type=Path, default=Path("/etc/camera-logs/native.env"))
    parser.add_argument("--init", action="store_true", help="仅生成配置供自定义日志路径、端口等")
    args = parser.parse_args(argv)
    if not args.config.is_absolute():
        raise ValueError("--config必须是绝对路径")
    config = args.config
    if config.is_symlink():
        raise ValueError("配置文件不能是符号链接")
    if not config.exists():
        if Path("/opt/camera-logs/.native-managed.json").exists():
            raise ValueError("检测到既有原生安装但配置丢失，请恢复原配置，不生成新密钥")
        create_config(config, args.component)
        print(f"已创建0600配置：{config}，未输出任何密码或令牌")
    if args.init:
        return 0
    if sys.platform != "linux" or os.geteuid() != 0 or not Path("/run/systemd/system").is_dir():
        raise ValueError("请在使用systemd的Linux主机以sudo/root运行")
    if config.stat().st_mode & 0o077:
        raise ValueError("配置含凭据，请先将文件权限设置为0600")
    values = read_config(config)
    validate(values, args.component)
    deploy(values, args.component, config, Path(__file__).resolve().parents[1])
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError:
        print("原生部署命令失败，请检查上方安装输出与systemd日志；已有数据已保留", file=sys.stderr)
        raise SystemExit(1) from None
    except (ValueError, RuntimeError, OSError) as error:
        # 配置验证错误仅含字段名，运行异常不输出完整环境或凭据。
        print(f"原生部署失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
