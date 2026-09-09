"""生成独立 systemd/Nginx/MongoDB 配置，不修改主机已有 Nginx 站点。"""

import json
from pathlib import Path


def environment(values):
    """systemd环境文件只作字面量，转义引号与反斜线防止凭据改变语义。"""
    return "".join(f'{key}="{value.replace(chr(92), chr(92)*2).replace(chr(34), chr(92)+chr(34))}"\n'
                   for key, value in values.items())


def service_unit(values, name, command, *, config=True):
    """常驻服务异常自动重启、开机自启，关闭留足采集排空时间。"""
    root, data = values["INSTALL_ROOT"], values["DATA_ROOT"]
    env = f"EnvironmentFile={root}/etc/{name}.env\n" if config else ""
    capabilities = "AmbientCapabilities=CAP_NET_BIND_SERVICE\n" if name == "frontend" and int(values["FRONTEND_PORT"]) < 1024 else ""
    return f"""# 由原生部署生成；自定义参数请修改native.env后重新部署。
[Unit]
Description=Camera Logs {name}
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User={values['SERVICE_USER']}
Group={values['SERVICE_USER']}
WorkingDirectory={data}
{env}ExecStart={command}
Restart=on-failure
RestartSec=5
TimeoutStopSec=120
KillSignal=SIGTERM
UMask=0027
LimitNOFILE=65536
NoNewPrivileges=true
{capabilities}\
PrivateTmp=true

[Install]
WantedBy=multi-user.target
"""


def render(values, source):
    """返回按服务分类的配置，部署器只发布用户选择的组件。"""
    root, data = values["INSTALL_ROOT"], values["DATA_ROOT"]
    api_values = values | {"LOG_ROOT": values["API_LOG_ROOT"]}
    worker_values = values | {"LOG_ROOT": values["LOG_ROOT"]}
    # 运行环境不需要数据库管理员明文和副本集key；MONGO_URI仍按授权连接所需保留。
    for env in (api_values, worker_values):
        for key in ("MONGO_ADMIN_PASSWORD", "MONGO_REPLICA_KEY"):
            env.pop(key, None)
    nginx_site = (Path(source) / "deploy/nginx/frontend.conf").read_text()
    nginx_site = nginx_site.replace("listen 80;", f"listen {values['FRONTEND_PORT']};")
    nginx_site = nginx_site.replace("/usr/share/nginx/html", f"{root}/frontend")
    nginx_site = nginx_site.replace("${BACKEND_UPSTREAM}", values["BACKEND_UPSTREAM"])
    nginx = f"""# 独立实例不读取/etc/nginx/nginx.conf或其它站点。
worker_processes auto;
pid {data}/nginx/nginx.pid;
error_log stderr warn;
events {{ worker_connections 4096; }}
http {{
    include /etc/nginx/mime.types;
    default_type application/octet-stream;
    access_log off;
    client_body_temp_path {data}/nginx/client;
    proxy_temp_path {data}/nginx/proxy;
    fastcgi_temp_path {data}/nginx/fastcgi;
    uwsgi_temp_path {data}/nginx/uwsgi;
    scgi_temp_path {data}/nginx/scgi;
{nginx_site}
}}
"""
    # JSON是YAML的子集；用结构化序列化保证路径和参数不会注入mongod配置。
    mongo = json.dumps({"storage": {"dbPath": values["MONGO_DATA_ROOT"]},
                        "net": {"bindIp": values["MONGO_BIND_IP"], "port": int(values["MONGO_PORT"])},
                        "replication": {"replSetName": values["MONGO_REPLICA_SET"]},
                        "security": {"authorization": "enabled", "keyFile": f"{root}/etc/mongo.key"}}, indent=2)
    return {
        "backend": {"api.env": environment(api_values), "api.service": service_unit(values, "api",
            f"{root}/venvs/backend/bin/python -m uvicorn camera_logs.main:app --host {values['API_BIND_IP']} --port {values['API_PORT']} --no-access-log")},
        "worker": {"worker.env": environment(worker_values), "worker.service": service_unit(values, "worker",
            f"{root}/venvs/worker/bin/python -m camera_logs.worker")},
        "frontend": {"nginx.conf": nginx, "frontend.service": service_unit(values, "frontend",
            f"/usr/sbin/nginx -c {root}/etc/nginx.conf -g \"daemon off;\"", config=False)},
        "database": {"mongod.conf": mongo, "mongo.key": values["MONGO_REPLICA_KEY"] + "\n",
                     "mongo.service": service_unit(values, "mongo", f"/usr/bin/mongod --config {root}/etc/mongod.conf", config=False)},
    }
