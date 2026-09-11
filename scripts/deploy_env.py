"""以标准库为 Linux 一键部署创建仅本机可读的初始环境文件。"""

import argparse
import base64
import os
import secrets
from pathlib import Path


def _fernet_key() -> str:
    """生成符合 Fernet 格式的 URL 安全 Base64 32 字节密钥。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")


def create_environment(path: Path, component: str = "all") -> None:
    """独占写入 0600 环境文件；已有文件由部署脚本保留且不会覆盖。"""
    values = {
        "ENCRYPTION_KEY": _fernet_key(),
        "BOOTSTRAP_TOKEN": secrets.token_urlsafe(48),
        "INTERNAL_TOKEN": secrets.token_urlsafe(48),
        "ADMIN_USERNAME": "admin",
        # 内置管理员首登例外使用固定初始密码，服务端会强制其首次登录后修改。
        "ADMIN_PASSWORD": "asdf!234",
        "SESSION_SECONDS": "28800",
        "SESSION_COOKIE_SECURE": "false",
        "DATABASE_NAME": "camera_logs",
        "LOG_ROOT": "data/logs",
        "NODE_CAPACITY": "100",
        "CLUSTER_CAPACITY": "500",
        "RETENTION_DAYS": "7",
    }
    if component == "frontend":
        values = {"BACKEND_UPSTREAM": "", "FRONTEND_PORT": "5175"}
    elif component == "backend":
        values.update(MONGO_URI="", API_BIND_IP="127.0.0.1", API_PORT="18080",
                      FORWARDED_ALLOW_IPS="127.0.0.1", API_DATA_ROOT="api-data")
    elif component == "worker":
        # 节点必须使用 API 的原密钥和令牌，不能生成一套不相容的凭据。
        values.update(MONGO_URI="", ENCRYPTION_KEY="", BOOTSTRAP_TOKEN="", INTERNAL_TOKEN="",
                      ADMIN_PASSWORD="", NODE_ID="", NODE_URL="", NODE_PORT="18081",
                      HOST_LOG_ROOT="worker-data", NFS_ROOT="/srv/camera-logs/nfs-coredump", NFS_SERVER_IP="")
    elif component == "database":
        values = {"DATABASE_HOST": "127.0.0.1", "DATABASE_BIND_IP": "127.0.0.1",
                  "MONGO_PORT_1": "27017", "MONGO_PORT_2": "27018", "MONGO_PORT_3": "27019",
                  "MONGO_ROOT_USERNAME": "camera_admin", "MONGO_ROOT_PASSWORD": secrets.token_urlsafe(36),
                  "MONGO_REPLICA_KEY": base64.b64encode(secrets.token_bytes(384)).decode(),
                  "MONGO_DATA_1": "mongo1-data", "MONGO_DATA_2": "mongo2-data", "MONGO_DATA_3": "mongo3-data"}
    elif component != "all":
        raise ValueError("未知部署组件")
    else:
        values.update(HOST_LOG_ROOT="worker-data", API_DATA_ROOT="api-data", FRONTEND_PORT="5175",
                      API_PORT="18080", COLLECTOR_NODE_ID="compose-worker-1", COLLECTOR_NODE_URL="http://worker:18081", NODE_PORT="18081",
                      MONGO_DATA_1="mongo1-data", MONGO_DATA_2="mongo2-data", MONGO_DATA_3="mongo3-data",
                      NFS_ROOT="/srv/camera-logs/nfs-coredump", NFS_SERVER_IP="",
                      MONGO_ROOT_USERNAME="camera_admin", MONGO_ROOT_PASSWORD=secrets.token_urlsafe(36),
                      MONGO_REPLICA_KEY=base64.b64encode(secrets.token_bytes(384)).decode())
    comments = {
        "HOST_LOG_ROOT": "可改为宿主机绝对路径：/srv/camera-logs/collector；设备日志在data/，运行日志在service-logs/。",
        "NFS_ROOT": "设备coredump的NFS总目录，必须是宿主机非根绝对路径；Worker容器以相同路径挂载并创建设备IP子目录。",
        "NFS_SERVER_IP": "设备可达的NFS宿主机IP；留空禁用NFS，不安装服务或修改exports。",
        "API_DATA_ROOT": "可改为宿主机绝对路径：/srv/camera-logs/api；不应与采集节点共用。",
        "MONGO_DATA_1": "可改为/srv/camera-logs/mongo1；三成员目录必须不同，已有数据不会自动搬迁。",
        "MONGO_DATA_2": "可改为/srv/camera-logs/mongo2；不能与其它成员共用目录。",
        "MONGO_DATA_3": "可改为/srv/camera-logs/mongo3；不能与其它成员共用目录。",
        "MONGO_URI": "必填：API和节点可达的MongoDB副本集地址；包含数据库用户名/密码、replicaSet=rs0、authSource=admin。",
        "MONGO_ROOT_USERNAME": "认证 MongoDB 管理员名称；密码中的 URI 保留字符由部署器编码，不要手工拼接 URI。",
        "MONGO_ROOT_PASSWORD": "认证 MongoDB 管理员密码；含 $ 时在 dotenv 使用单引号，避免 Compose 展开宿主机变量。",
        "MONGO_REPLICA_KEY": "认证副本集成员共享密钥；同一平台的三个成员必须使用同一值。",
        "ENCRYPTION_KEY": "必须与同一平台的 API 和各节点一致；不要在节点侧另行生成。",
        "BOOTSTRAP_TOKEN": "必须与后端一致；服务账号凭据，勿公开或写入Git。",
        "INTERNAL_TOKEN": "必须与后端一致；节点内部接口认证凭据，勿公开。",
        "DATABASE_HOST": "跨机部署改为数据库服务器内网IPv4或DNS；此地址必须被所有成员/API/节点访问。",
        "DATABASE_BIND_IP": "跨机部署改为数据库服务器内网IP或0.0.0.0；同时修改DATABASE_HOST为可达地址。",
        "BACKEND_UPSTREAM": "必填：http://后端内网IP:18080；无路径/凭据。127.0.0.1在前端容器内指向自身。",
        "FORWARDED_ALLOW_IPS": "可修改：可信前端代理来源IP/CIDR，逗号分隔；不是设备IP白名单。",
        "API_BIND_IP": "跨机前端访问时改为后端内网IP或0.0.0.0。",
        "API_PORT": "可修改：后端监听端口1-65535，与前端BACKEND_UPSTREAM端口一致。",
        "FRONTEND_PORT": "可修改：平台HTTP对外访问端口1-65535。",
        "NODE_ID": "必填：稳定唯一节点名，如collector-01；已有日志后不得随意改变。",
        "COLLECTOR_NODE_ID": "完整部署内置采集节点的稳定ID；后台登记须使用此ID；.env.worker不会被完整部署读取。已有日志后勿改名。",
        "COLLECTOR_NODE_URL": "完整部署保留http://worker:18081；API容器通过Docker服务名访问节点，不要改为127.0.0.1。",
        "NODE_URL": "必填：http://节点内网IP:18081，后端必须能访问，端口与NODE_PORT一致。",
        "NODE_PORT": "可修改：采集节点内部接口监听端口1-65535。",
        "RETENTION_DAYS": "可修改：默认保留天数；已保存后台配置时以后者为准。",
        "NODE_CAPACITY": "可修改：单节点任务容量，按磁盘和压测能力调整。",
        "ADMIN_PASSWORD": "仅初始化空库；已修改的管理员密码不会被重复部署覆盖。",
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write("# 一键部署初始配置；含凭据，仅限本机管理员读取。\n")
        output.write(f"# 组件：{component}；各项含义和可修改示例见 deploy/config/{component}.env.example。\n")
        if component == "all":
            output.write("# 完整部署只读取本文件，已包含一个worker；独立.env.worker只供deploy-worker.sh使用。\n")
        elif component == "worker":
            output.write("# 仅用于额外独立节点；必须补齐下列原平台凭据和可达数据库地址，登记节点本身不会启动服务。\n")
        output.write("# 目录填绝对路径使用宿主机存储；保留默认名称使用 Docker 命名卷。\n")
        for key, value in values.items():
            if key in comments:
                output.write(f"# {comments[key]}\n")
            output.write(f"{key}={value}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(".env"))
    parser.add_argument("--component", choices=("all", "frontend", "backend", "worker", "database"), default="all")
    args = parser.parse_args()
    create_environment(args.output, args.component)
    print("部署环境文件已创建，未输出任何密钥或密码")
