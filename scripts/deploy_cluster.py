"""Docker 跨机部署的内部配置解析、URI 生成与预检，不执行 shell 配置。"""

import ipaddress
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from deploy_component import endpoint

SHARED_KEYS = ("MONGO_URI", "ENCRYPTION_KEY", "BOOTSTRAP_TOKEN", "INTERNAL_TOKEN")
_COMPOSE_HOSTS = frozenset({"mongo1", "mongo2", "mongo3", "api", "worker", "frontend"})
_KEY = re.compile(r"(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=(.*)")


def _decode_double_quoted(value: str) -> str:
    """仅展开 Docker dotenv 双引号支持的转义，保留 UTF-8 字符和未知转义。"""
    escapes = {"n": "\n", "r": "\r", "t": "\t", "\\": "\\", '"': '"'}
    output: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            next_character = value[index + 1]
            if next_character in escapes:
                output.append(escapes[next_character])
                index += 2
                continue
        output.append(character)
        index += 1
    return "".join(output)


def _decode_single_quoted(value: str) -> str:
    """展开 dotenv 单引号中的引号和反斜杠转义，其余内容始终为字面量。"""
    output: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value) and value[index + 1] in {"\\", "'"}:
            output.append(value[index + 1])
            index += 2
            continue
        output.append(character)
        index += 1
    return "".join(output)


def read_environment(path: Path) -> dict[str, str]:
    """解析 Docker dotenv 的静态 KEY=VALUE，支持引号和特殊字符但不展开变量。"""
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        matched = _KEY.fullmatch(line)
        if not matched:
            raise ValueError(f"配置行 {number} 不是合法的 KEY=VALUE")
        key, value = matched.groups()
        value = value.strip()
        if value[:1] in {"'", '"'}:
            mark = value[0]
            if len(value) < 2 or not value.endswith(mark):
                raise ValueError(f"配置行 {number} 的引号未闭合")
            value = value[1:-1]
            if mark == '"':
                value = _decode_double_quoted(value)
            else:
                value = _decode_single_quoted(value)
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].rstrip()
        values[key] = value
    return values


def _require(values, *names):
    """拒绝空项和文档占位项，避免将未完成合同传入 Compose。"""
    missing = [name for name in names if not values.get(name) or "REPLACE_" in values[name]]
    if missing:
        raise ValueError(f"请填写跨机部署配置：{', '.join(missing)}")


def _hosts(uri: str) -> list[str]:
    """只从 URI 的网络位置读取主机，用户名、密码和查询参数不会参与判断。"""
    parsed = urlsplit(uri)
    entries = parsed.netloc.rsplit("@", 1)[-1].split(",") if parsed.scheme == "mongodb" else [parsed.netloc]
    return [urlsplit(f"//{entry}").hostname or "" for entry in entries]


def _routable_host(host: str, name: str):
    """跨机地址不能使用回环或通配监听地址。"""
    if not host or host.lower() == "localhost" or host in {"0.0.0.0", "::"}:
        raise ValueError(f"{name} 必须是跨机可达的主机地址")
    try:
        if ipaddress.ip_address(host).is_loopback:
            raise ValueError(f"{name} 必须是跨机可达的主机地址")
    except ValueError as error:
        if "跨机" in str(error):
            raise
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", host):
            raise ValueError(f"{name} 必须是合法 IPv4、IPv6 或 DNS 主机名") from error


def _validate_uri(values, *, members: set[int]):
    """验证 A/B 共享的认证 rs0 URI，允许 B 使用原生单成员或 A 的三成员。"""
    _require(values, *SHARED_KEYS)
    uri = values["MONGO_URI"]
    parsed = urlsplit(uri)
    if parsed.scheme != "mongodb" or "@" not in parsed.netloc:
        raise ValueError("MONGO_URI 必须是带认证的 mongodb:// URI")
    hosts = _hosts(uri)
    if len(hosts) not in members or any(not host for host in hosts):
        raise ValueError("MONGO_URI 成员数量不符合跨机部署合同")
    for host in hosts:
        if host.lower() in _COMPOSE_HOSTS:
            raise ValueError("MONGO_URI 不得使用 Compose 私网主机名")
        _routable_host(host, "MONGO_URI 成员")
    query = parse_qs(parsed.query)
    if not parsed.path.rstrip("/") or query.get("replicaSet") != ["rs0"] or query.get("authSource") != ["admin"]:
        raise ValueError("MONGO_URI 必须包含数据库、replicaSet=rs0 和 authSource=admin")
    return unquote(parsed.path.lstrip("/"))


def _validate_worker(values):
    """验证每台 Worker 的独立地址和本机持久化根目录。"""
    _require(values, "NODE_ID", "NODE_URL", "NODE_PORT", "HOST_LOG_ROOT")
    endpoint(values["NODE_URL"], "NODE_URL")
    host = urlsplit(values["NODE_URL"]).hostname or ""
    if host.lower() in _COMPOSE_HOSTS:
        raise ValueError("NODE_URL 不得使用 Compose 私网主机名")
    _routable_host(host, "NODE_URL")
    try:
        port = int(values["NODE_PORT"])
        published = urlsplit(values["NODE_URL"]).port or 80
    except ValueError as error:
        raise ValueError("NODE_PORT 必须是合法端口") from error
    if not 1 <= port <= 65535 or port != published:
        raise ValueError("NODE_URL 端口必须与合法的 NODE_PORT 一致")
    if not Path(values["HOST_LOG_ROOT"]).is_absolute() or values["HOST_LOG_ROOT"] == "/":
        raise ValueError("HOST_LOG_ROOT 必须是非根绝对路径")


def _replace_value(path: Path, key: str, value: str):
    """原子替换 dotenv 单项，JSON 双引号保留 Compose 可读取的字面值。"""
    if path.is_symlink():
        raise ValueError("跨机部署配置不能是符号链接")
    lines = path.read_text(encoding="utf-8").splitlines()
    rendered = f"{key}={json.dumps(value, ensure_ascii=True)}"
    for index, line in enumerate(lines):
        matched = _KEY.fullmatch(line.strip())
        if matched and matched.group(1) == key:
            lines[index] = rendered
            break
    else:
        lines.append(rendered)
    temporary = path.with_suffix(path.suffix + ".new")
    created = False
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    created = True
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write("\n".join(lines) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def initialize_multi_host(path: Path):
    """将常规全平台配置标记为跨机，并预置 A 数据库与本机 Worker 所需字段。"""
    additions = """\n# 跨机拓扑：服务器A完整平台，服务器B仅Worker；部署器会以管理员密码URL编码生成认证MONGO_URI。\nDEPLOY_TOPOLOGY=multi-host\n# 填写服务器A对B可达的IPv4或DNS地址；DATABASE_BIND_IP可为A内网IPv4或0.0.0.0。\nDATABASE_HOST=\nDATABASE_BIND_IP=\nMONGO_PORT_1=27017\nMONGO_PORT_2=27018\nMONGO_PORT_3=27019\n# A的API和前端代理也必须使用可达地址，不能保留容器服务名。\nAPI_BIND_IP=\nAPI_PORT=8000\nBACKEND_UPSTREAM=\nFORWARDED_ALLOW_IPS=\n# A本机Worker必须使用A可达的稳定地址，不能保留worker容器名。\nNODE_ID=collector-a-01\nNODE_URL=\nNODE_PORT=8001\nNODE_BIND_IP=0.0.0.0\n"""
    with path.open("a", encoding="utf-8") as output:
        output.write(additions)
    path.chmod(0o600)


def initialize_worker_multi_host(path: Path):
    """将既有独立 Worker 配置标记为跨机接入，不添加 A 的数据库字段。"""
    with path.open("a", encoding="utf-8") as output:
        output.write("\n# 跨机拓扑：本机仅运行Worker，MONGO_URI和三项共享密钥必须来自服务器A。\nDEPLOY_TOPOLOGY=multi-host\n")
    path.chmod(0o600)


def prepare_platform_environment(path: Path):
    """由 A 数据库参数构造唯一 URI，再校验完整平台与本机 Worker 合同。"""
    values = read_environment(path)
    _require(values, "DATABASE_HOST", "DATABASE_BIND_IP", "MONGO_PORT_1", "MONGO_PORT_2", "MONGO_PORT_3", "MONGO_ROOT_USERNAME", "MONGO_ROOT_PASSWORD", "MONGO_REPLICA_KEY", "DATABASE_NAME")
    _routable_host(values["DATABASE_HOST"], "DATABASE_HOST")
    try:
        if ipaddress.ip_address(values["DATABASE_HOST"]).version == 6:
            raise ValueError("DATABASE_HOST 当前 Docker 数据库 Compose 仅支持 IPv4 或 DNS 名称")
    except ValueError as error:
        if "当前 Docker" in str(error):
            raise
    if all(item.strip() in {"127.0.0.1", "::1", "localhost"} for item in values["DATABASE_BIND_IP"].split(",")):
        raise ValueError("DATABASE_BIND_IP 不能只监听回环地址")
    try:
        ports = [int(values[f"MONGO_PORT_{index}"]) for index in (1, 2, 3)]
    except ValueError as error:
        raise ValueError("MongoDB 端口必须是整数") from error
    if len(set(ports)) != 3 or any(port < 1 or port > 65535 for port in ports):
        raise ValueError("三个 MongoDB 端口必须合法且互不相同")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", values["DATABASE_NAME"]):
        raise ValueError("DATABASE_NAME 只能使用字母、数字、下划线和连字符")
    hosts = ",".join(f"{values['DATABASE_HOST']}:{port}" for port in ports)
    uri = f"mongodb://{quote(values['MONGO_ROOT_USERNAME'], safe='')}:{quote(values['MONGO_ROOT_PASSWORD'], safe='')}@{hosts}/{values['DATABASE_NAME']}?replicaSet=rs0&authSource=admin"
    candidate = values | {"MONGO_URI": uri}
    _validate_uri(candidate, members={3})
    _validate_worker(candidate)
    _require(candidate, "API_BIND_IP", "API_PORT", "BACKEND_UPSTREAM")
    _routable_host(urlsplit(candidate["BACKEND_UPSTREAM"]).hostname or "", "BACKEND_UPSTREAM")
    _replace_value(path, "MONGO_URI", uri)
    return candidate


def prepare_authenticated_single_host_environment(path: Path):
    """从单机认证配置生成仅容器网络使用的认证 URI，不改写用户输入的密码。"""
    values = read_environment(path)
    _require(values, "MONGO_ROOT_USERNAME", "MONGO_ROOT_PASSWORD", "MONGO_REPLICA_KEY", "DATABASE_NAME")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", values["DATABASE_NAME"]):
        raise ValueError("DATABASE_NAME 只能使用字母、数字、下划线和连字符")
    uri = (
        f"mongodb://{quote(values['MONGO_ROOT_USERNAME'], safe='')}:"
        f"{quote(values['MONGO_ROOT_PASSWORD'], safe='')}@"
        f"mongo1:27017,mongo2:27017,mongo3:27017/{values['DATABASE_NAME']}"
        "?replicaSet=rs0&authSource=admin"
    )
    _replace_value(path, "COMPOSE_MONGO_URI", uri)
    return values | {"COMPOSE_MONGO_URI": uri}


def validate_worker_environment(path: Path):
    """验证 B 的 Worker 仅接入已存在平台，不创建或改变 A 的数据库配置。"""
    values = read_environment(path)
    _require(values, "DATABASE_NAME")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", values["DATABASE_NAME"]):
        raise ValueError("DATABASE_NAME 只能使用字母、数字、下划线和连字符")
    database_name = _validate_uri(values, members={1, 3})
    if values["DATABASE_NAME"] != database_name:
        raise ValueError("DATABASE_NAME 必须与 MONGO_URI 中的数据库名一致")
    _validate_worker(values)
    return values


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("init", "worker-init", "topology", "platform", "single-host", "worker"))
    parser.add_argument("env_file", type=Path)
    args = parser.parse_args()
    try:
        if args.mode == "init":
            initialize_multi_host(args.env_file)
        elif args.mode == "worker-init":
            initialize_worker_multi_host(args.env_file)
        elif args.mode == "topology":
            print(read_environment(args.env_file).get("DEPLOY_TOPOLOGY", ""))
        elif args.mode == "platform":
            prepare_platform_environment(args.env_file)
        elif args.mode == "single-host":
            prepare_authenticated_single_host_environment(args.env_file)
        else:
            validate_worker_environment(args.env_file)
    except (OSError, ValueError) as error:
        parser.exit(1, f"跨机部署预检未通过：{error}\n")
