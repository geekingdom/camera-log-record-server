"""原生部署配置：只解析数据，不 source 或执行配置中的任何文本。"""

import base64
import ipaddress
import os
import re
import secrets
from pathlib import Path
from urllib.parse import quote, urlsplit

from deploy_component import endpoint


def defaults():
    """为单机首次部署生成独立凭据；已存在配置绝不调用此函数覆盖。"""
    password = secrets.token_urlsafe(36)
    return {
        "INSTALL_ROOT": "/opt/camera-logs", "DATA_ROOT": "/var/lib/camera-logs",
        "LOG_ROOT": "/var/lib/camera-logs/collector", "API_LOG_ROOT": "/var/lib/camera-logs/api",
        "NFS_ROOT": "/srv/camera-logs/nfs-coredump", "NFS_SERVER_IP": "",
        "MONGO_DATA_ROOT": "/var/lib/camera-logs/mongo", "SERVICE_USER": "camera-logs",
        "PYTHON_BIN": "python3.12", "INSTALL_PACKAGES": "true",
        "MONGO_BIND_IP": "127.0.0.1", "MONGO_ADVERTISED_HOST": "127.0.0.1",
        "MONGO_PORT": "27017", "MONGO_REPLICA_SET": "rs0",
        "MONGO_ADMIN_USER": "camera_admin", "MONGO_ADMIN_PASSWORD": password,
        "MONGO_REPLICA_KEY": base64.b64encode(secrets.token_bytes(384)).decode(),
        "MONGO_URI": f"mongodb://camera_admin:{quote(password, safe='')}@127.0.0.1:27017/camera_logs?replicaSet=rs0&authSource=admin",
        "DATABASE_NAME": "camera_logs", "API_BIND_IP": "127.0.0.1", "API_PORT": "8000",
        "FRONTEND_PORT": "5173", "BACKEND_UPSTREAM": "http://127.0.0.1:8000",
        "FORWARDED_ALLOW_IPS": "127.0.0.1", "NODE_ID": "native-worker-1",
        "NODE_URL": "http://127.0.0.1:8001", "NODE_BIND_IP": "127.0.0.1", "NODE_PORT": "8001",
        "ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
        "BOOTSTRAP_TOKEN": secrets.token_urlsafe(48), "INTERNAL_TOKEN": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "asdf!234",
        "SESSION_COOKIE_SECURE": "false", "RETENTION_DAYS": "7",
        "NODE_CAPACITY": "100", "CLUSTER_CAPACITY": "500", "PSH_MODE": "disabled",
    }


COMMENTS = {
    "INSTALL_ROOT": "可修改：程序、虚拟环境和生成的服务配置目录，必须绝对路径。",
    "DATA_ROOT": "可修改：数据根目录；其它 *_ROOT 独立配置，不随此项自动改变。",
    "LOG_ROOT": "可修改：设备日志、10 MiB 分卷和小时归档保存路径；迁移前先停止节点并搬迁原数据。",
    "API_LOG_ROOT": "可修改：API 运行日志及导出临时文件目录，不与采集路径共用。",
    "NFS_ROOT": "设备coredump的NFS总目录；必须是非根绝对路径，Worker会在此创建设备IP子目录。",
    "NFS_SERVER_IP": "设备可达的宿主机NFS地址；留空禁用NFS，不安装服务或修改exports。",
    "MONGO_DATA_ROOT": "可修改：MongoDB 数据目录；已有数据库不能换空目录后继续使用原平台。",
    "SERVICE_USER": "可修改：专用非 root 系统用户，脚本自动创建；不要复用人工登录用户。",
    "PYTHON_BIN": "可修改：已有 Python 3.12+ 路径；找不到则通过 uv 安装隔离 Python 3.12。",
    "INSTALL_PACKAGES": "默认 true 自动配置官方 MongoDB APT 源并安装依赖；预装依赖环境可设 false。",
    "MONGO_BIND_IP": "可修改：逗号分隔监听IP，初始化需包含127.0.0.1；跨机加数据库内网IP。",
    "MONGO_ADVERTISED_HOST": "可修改：副本集公布的IPv4或DNS，所有客户端必须可达；初始化后不可直接修改。",
    "MONGO_PORT": "可修改：MongoDB端口；必须同时更新MONGO_URI。",
    "MONGO_ADMIN_USER": "部署数据库账号（admin库root角色）；可自行使用外部最小权限账号部署API/节点。",
    "MONGO_ADMIN_PASSWORD": "首次生成；已有数据库不能随意修改，脚本不会重置数据库密码。",
    "MONGO_REPLICA_KEY": "首次生成的MongoDB成员认证密钥，重跑必须保留。",
    "MONGO_URI": "API/节点共用副本集URI，跨机修改地址；账号密码中的特殊字符需URL编码。",
    "API_BIND_IP": "可修改：远程前端访问时改为后端内网IP；仅信任配置的代理地址。",
    "API_PORT": "可修改：后端端口，同时修改BACKEND_UPSTREAM。",
    "FRONTEND_PORT": "可修改：浏览器访问的HTTP端口；HTTPS需另配受控TLS代理。",
    "BACKEND_UPSTREAM": "可修改：http://后端内网IP:端口，不含路径，原生进程的127.0.0.1为本机。",
    "FORWARDED_ALLOW_IPS": "可修改：可信前端代理的IP/CIDR逗号分隔，禁止星号；与设备目标IP无关。",
    "NODE_ID": "可修改：稳定唯一节点ID，已有采集数据后不要随意变更。",
    "NODE_URL": "可修改：http://节点内网IP:端口，必须能被API访问。",
    "NODE_BIND_IP": "可修改：跨机时设节点内网IP；同时更新NODE_URL。",
    "ENCRYPTION_KEY": "必须与同平台API/所有节点一致；丢失后无法解密已有设备密码。",
    "INTERNAL_TOKEN": "必须与同平台API/所有节点一致，不要在日志或Git中公开。",
    "BOOTSTRAP_TOKEN": "服务账号令牌，妥善保管；独立节点应复制平台配置。",
    "ADMIN_PASSWORD": "按要求仅空库首次使用默认口令，首登强制修改；重跑不会覆盖已改密码。",
    "RETENTION_DAYS": "可修改：默认保留天数；后台已经保存的设置优先。",
    "PSH_MODE": "disabled/mock/remote；调试mock密码放独立受限文件，勿写入仓库。",
}


def create_config(path, component="all"):
    """独占创建 0600 配置；分组件需要显式填写远程端点，避免误用本机地址。"""
    path = Path(path)
    values = defaults()
    if component in {"backend", "worker"}:
        values["MONGO_URI"] = ""
    if component == "worker":
        for key in ("ENCRYPTION_KEY", "INTERNAL_TOKEN", "BOOTSTRAP_TOKEN", "NODE_URL", "NODE_ID"):
            values[key] = ""
    if component == "frontend":
        values["BACKEND_UPSTREAM"] = ""
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        stream.write("# 原生Linux部署配置；纯KEY=VALUE，不支持引号、变量展开或行尾注释。\n")
        stream.write("# 可修改项见逐项中文说明；重复运行保留本文件及全部日志。\n")
        for key, value in values.items():
            stream.write(f"# {COMMENTS.get(key, '可修改：平台运行参数，详见docs/native-deployment.md。')}\n{key}={value}\n")


def read_config(path):
    """读取单行值并拒绝重复键、控制字符；错误仅报告行号，不回显凭据。"""
    result = {}
    for number, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in result:
            raise ValueError(f"部署配置第{number}行无效或键重复")
        if any(ord(c) < 32 for c in value) or value != value.strip():
            raise ValueError(f"部署配置第{number}行含控制字符或首尾空白")
        result[key] = value
    return result


def validate(values, component):
    """在安装和写服务配置前验证路径、监听地址、权限边界及必填凭据。"""
    required = {"INSTALL_ROOT", "DATA_ROOT", "SERVICE_USER", "INSTALL_PACKAGES"}
    if component in {"all", "database"}:
        required.update({"MONGO_ADMIN_USER", "MONGO_ADMIN_PASSWORD", "MONGO_REPLICA_KEY",
                         "MONGO_REPLICA_SET", "MONGO_ADVERTISED_HOST", "MONGO_DATA_ROOT"})
    if component in {"all", "backend", "worker"}:
        required.update({"MONGO_URI", "ENCRYPTION_KEY", "BOOTSTRAP_TOKEN", "INTERNAL_TOKEN", "DATABASE_NAME"})
    for key in required:
        if not values.get(key):
            raise ValueError(f"请填写部署配置 {key}")
    for key, value in values.items():
        if key.endswith("_ROOT") and (not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or value == "/" or ".." in Path(value).parts):
            raise ValueError(f"{key} 必须是无空格的非根绝对路径")
        if key.endswith("_PORT") and (not value.isdigit() or not 1 <= int(value) <= 65535):
            raise ValueError(f"{key} 端口必须为1到65535")
    if values["SERVICE_USER"] == "root" or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", values["SERVICE_USER"]):
        raise ValueError("SERVICE_USER 必须是专用非root系统账号")
    if values["INSTALL_PACKAGES"] not in {"true", "false"}:
        raise ValueError("INSTALL_PACKAGES 只能为true或false")
    for key in ("API_BIND_IP", "NODE_BIND_IP"):
        ipaddress.ip_address(values[key])
    for address in values["MONGO_BIND_IP"].split(","):
        ipaddress.ip_address(address)
    if component in {"all", "database"}:
        if "127.0.0.1" not in values["MONGO_BIND_IP"].split(","):
            raise ValueError("MONGO_BIND_IP 必须包含127.0.0.1以初始化本地认证")
        for key in ("MONGO_REPLICA_SET", "MONGO_ADVERTISED_HOST", "DATABASE_NAME"):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", values[key]):
                raise ValueError(f"{key} 仅允许字母数字点下划线及短横线")
        if not re.fullmatch(r"[A-Za-z0-9+/=]{6,1024}", values["MONGO_REPLICA_KEY"]):
            raise ValueError("MongoDB成员密钥格式错误")
    if component in {"all", "frontend"}:
        endpoint(values.get("BACKEND_UPSTREAM", ""), "BACKEND_UPSTREAM")
    if component in {"all", "worker"}:
        endpoint(values.get("NODE_URL", ""), "NODE_URL")
        node = urlsplit(values["NODE_URL"])
        if (node.port or (443 if node.scheme == "https" else 80)) != int(values["NODE_PORT"]):
            raise ValueError("NODE_URL端口必须与NODE_PORT一致")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", values.get("NODE_ID", "")):
            raise ValueError("NODE_ID 不能为空且只能使用安全名称")
        nfs_address = values.get("NFS_SERVER_IP", "")
        if nfs_address:
            try:
                address = ipaddress.ip_address(nfs_address)
            except ValueError as error:
                raise ValueError("启用NFS时NFS_SERVER_IP必须是设备可达IP地址") from error
            if address.is_unspecified or address.is_loopback:
                raise ValueError("NFS_SERVER_IP不能为通配或回环地址")
    if component in {"all", "backend", "worker"}:
        if not values["MONGO_URI"].startswith(("mongodb://", "mongodb+srv://")):
            raise ValueError("MONGO_URI 必须是副本集连接地址")
        if len(base64.urlsafe_b64decode(values["ENCRYPTION_KEY"])) != 32:
            raise ValueError("ENCRYPTION_KEY 必须为Fernet密钥")
    for address in values["FORWARDED_ALLOW_IPS"].split(","):
        ipaddress.ip_network(address, strict=False)
    if component == "all" and len({values[key] for key in ("API_PORT", "NODE_PORT", "MONGO_PORT", "FRONTEND_PORT")}) != 4:
        raise ValueError("完整单机部署的四个服务端口不能重复")
