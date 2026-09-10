"""初始化 Ubuntu/Debian 原生单成员 MongoDB 副本集和唯一应用管理员账号。

本脚本假定 ``camera-logs-mongo`` systemd 服务已由部署入口启动，且 mongod 已启用
authorization 与 keyFile。首次运行借助 localhost exception 初始化副本集和创建账号；
重复运行必须使用配置中的既有账号认证，只做拓扑核验，不会重配副本集或覆盖未知数据。
"""

import argparse
import re
import time
from pathlib import Path
from urllib.parse import quote

from native_config import read_config
from pymongo import MongoClient
from pymongo.errors import OperationFailure, PyMongoError

_SYSTEM_DATABASES = {"admin", "config", "local"}
_HOST = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_DATABASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,252}")


def _operation_code(error):
    """兼容 PyMongo 异常对象和测试替身，读取不包含凭据的 Mongo 错误码。"""
    return getattr(error, "code", None)


def _settings(values):
    """验证显式管理员和库名，补齐单成员部署的非敏感默认配置。"""
    result = {
        "MONGO_BIND_IP": values.get("MONGO_BIND_IP", "127.0.0.1"),
        "MONGO_PORT": values.get("MONGO_PORT", "27017"),
        "MONGO_REPLICA_SET": values.get("MONGO_REPLICA_SET", "rs0"),
        "MONGO_DATA_ROOT": values.get("MONGO_DATA_ROOT", "/var/lib/camera-logs/mongo"),
        "MONGO_ADVERTISED_HOST": values.get("MONGO_ADVERTISED_HOST", "127.0.0.1"),
        "MONGO_ADMIN_USER": values.get("MONGO_ADMIN_USER", ""),
        "MONGO_ADMIN_PASSWORD": values.get("MONGO_ADMIN_PASSWORD", ""),
        "DATABASE_NAME": values.get("DATABASE_NAME", ""),
    }
    for name in ("MONGO_ADMIN_USER", "MONGO_ADMIN_PASSWORD", "DATABASE_NAME"):
        if not result[name] or "REPLACE_" in result[name]:
            raise ValueError(f"请在配置中填写 {name}")
    try:
        result["MONGO_PORT"] = int(result["MONGO_PORT"])
    except (TypeError, ValueError) as error:
        raise ValueError("MONGO_PORT 必须是 1 到 65535 之间的整数") from error
    if not 1 <= result["MONGO_PORT"] <= 65535:
        raise ValueError("MONGO_PORT 必须是 1 到 65535 之间的整数")
    if not _HOST.fullmatch(result["MONGO_ADVERTISED_HOST"]):
        raise ValueError("MONGO_ADVERTISED_HOST 必须是 IPv4 或 DNS 主机名；初版不支持 IPv6")
    if not _HOST.fullmatch(result["MONGO_REPLICA_SET"]):
        raise ValueError("MONGO_REPLICA_SET 必须是安全名称")
    if not _DATABASE.fullmatch(result["DATABASE_NAME"]):
        raise ValueError("DATABASE_NAME 必须是安全名称")
    return result


def _uri(settings, *, authenticated):
    """构造仅用于本机初始化的 directConnection URI，密码不会写入日志或异常消息。"""
    host, port = "127.0.0.1", settings["MONGO_PORT"]
    if not authenticated:
        return f"mongodb://{host}:{port}/?directConnection=true"
    user = quote(settings["MONGO_ADMIN_USER"], safe="")
    password = quote(settings["MONGO_ADMIN_PASSWORD"], safe="")
    return f"mongodb://{user}:{password}@{host}:{port}/admin?authSource=admin&directConnection=true"


def _replica_config(settings):
    """生成不可重配的单成员副本集配置，成员地址使用可从其它主机访问的公告地址。"""
    return {"_id": settings["MONGO_REPLICA_SET"], "members": [{
        "_id": 0, "host": f'{settings["MONGO_ADVERTISED_HOST"]}:{settings["MONGO_PORT"]}',
    }]}


def _same_replica(actual, expected):
    """只接受完全相同的单成员名称和地址，避免部署脚本覆盖已有拓扑。"""
    members = actual.get("members", [])
    return actual.get("_id") == expected["_id"] and len(members) == 1 and members[0].get("_id") == 0 \
        and members[0].get("host") == expected["members"][0]["host"]


def reconfigure_advertised_host(values, client_factory=MongoClient, *, timeout_seconds=30):
    """将受管单成员从回环公告地址迁移到配置的内网地址，不创建账号或修改数据。"""
    settings = _settings(values)
    expected = _replica_config(settings)
    client = client_factory(_uri(settings, authenticated=True), serverSelectionTimeoutMS=5000)
    try:
        admin = client.admin
        try:
            admin.command("connectionStatus")
            actual = admin.command("replSetGetConfig")["config"]
        except OperationFailure as error:
            raise RuntimeError("数据库管理员凭据核验失败") from error
        if actual.get("_id") != expected["_id"] or len(actual.get("members", [])) != 1 or actual["members"][0].get("_id") != 0:
            raise RuntimeError("仅支持迁移受管单成员副本集")
        if _same_replica(actual, expected):
            return {"changed": False, "host": settings["MONGO_ADVERTISED_HOST"], "port": settings["MONGO_PORT"]}
        previous = str(actual["members"][0].get("host", ""))
        old_host = previous.rsplit(":", 1)[0]
        if old_host not in {"127.0.0.1", "localhost"}:
            raise RuntimeError("已有副本集公布地址不是回环地址，拒绝自动迁移")
        migrated = dict(actual)
        migrated["version"] = int(actual.get("version", 1)) + 1
        migrated["members"] = [dict(actual["members"][0], host=expected["members"][0]["host"])]
        try:
            admin.command({"replSetReconfig": migrated})
        except OperationFailure as error:
            raise RuntimeError("副本集公告地址迁移失败") from error
        _wait_primary(admin, timeout_seconds)
        verified = admin.command("replSetGetConfig")["config"]
        if not _same_replica(verified, expected):
            raise RuntimeError("副本集公告地址迁移后核验失败")
        return {"changed": True, "host": settings["MONGO_ADVERTISED_HOST"], "port": settings["MONGO_PORT"]}
    finally:
        client.close()


def _wait_primary(admin, timeout_seconds):
    """等待单成员当选 PRIMARY；服务未就绪时停止初始化且不重试写入。"""
    deadline = time.monotonic() + timeout_seconds
    while True:
        if admin.command("hello").get("isWritablePrimary"):
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("副本集未在限定时间内成为 PRIMARY")
        time.sleep(0.2)


def _verify_known_instance(admin, expected):
    """已有凭据成功认证后只读取并核验副本集，不创建用户或数据库内容。"""
    try:
        admin.command("connectionStatus")
        replica = admin.command("replSetGetConfig")["config"]
    except OperationFailure as error:
        raise _CredentialsRejected("数据库管理员凭据核验失败") from error
    if not _same_replica(replica, expected):
        raise RuntimeError("已有副本集与配置不一致，拒绝重配")


class _CredentialsRejected(RuntimeError):
    """既有管理员凭据不能认证；只有受管空目录可显式进入首次初始化流程。"""


def initialize(values, client_factory=MongoClient, *, allow_initialize=False, timeout_seconds=30):
    """核验既有实例；仅显式受管空目录可初始化单成员副本集和 root 单账号。"""
    settings = _settings(values)
    expected = _replica_config(settings)
    authenticated_client = client_factory(_uri(settings, authenticated=True), serverSelectionTimeoutMS=5000)
    try:
        _verify_known_instance(authenticated_client.admin, expected)
        return {"replicaSet": settings["MONGO_REPLICA_SET"], "host": settings["MONGO_ADVERTISED_HOST"],
                "port": settings["MONGO_PORT"], "database": settings["DATABASE_NAME"], "created": False}
    except RuntimeError as error:
        if not isinstance(error, _CredentialsRejected):
            raise
        if not allow_initialize:
            raise RuntimeError("管理员凭据核验失败；仅受管空目录可使用 --allow-initialize") from error
    finally:
        authenticated_client.close()

    local_client = client_factory(_uri(settings, authenticated=False), serverSelectionTimeoutMS=5000)
    try:
        # 未初始化成员不能执行 listDatabases；rs.initiate 成功后 localhost exception 才可安全检查。
        try:
            local_client.admin.command({"replSetInitiate": expected})
        except OperationFailure as error:
            if _operation_code(error) != 23:
                raise RuntimeError("副本集初始化失败") from error
            try:
                replica = local_client.admin.command("replSetGetConfig")["config"]
            except OperationFailure as config_error:
                raise RuntimeError("已有副本集但管理员凭据核验失败，拒绝修改") from config_error
            if not _same_replica(replica, expected):
                raise RuntimeError("已有副本集与配置不一致，拒绝重配") from error
        _wait_primary(local_client.admin, timeout_seconds)
        try:
            databases = set(local_client.list_database_names())
        except PyMongoError as error:
            raise RuntimeError("无法确认数据库为空，拒绝使用 localhost exception") from error
        if databases - _SYSTEM_DATABASES:
            raise RuntimeError("检测到未知业务数据库，拒绝创建管理员账号")
        try:
            local_client.admin.command({"createUser": settings["MONGO_ADMIN_USER"],
                                        "pwd": settings["MONGO_ADMIN_PASSWORD"], "roles": ["root"]})
        except OperationFailure as error:
            raise RuntimeError("管理员账号创建失败，拒绝覆盖既有账号") from error
    finally:
        local_client.close()
    return {"replicaSet": settings["MONGO_REPLICA_SET"], "host": settings["MONGO_ADVERTISED_HOST"],
            "port": settings["MONGO_PORT"], "database": settings["DATABASE_NAME"], "created": True}


def main(argv=None):
    """读取配置并输出无凭据的初始化结果；错误只显示固定中文说明。"""
    parser = argparse.ArgumentParser(description="初始化 camera-logs-mongo 单成员副本集和管理员账号")
    parser.add_argument("--config", required=True, type=Path, help="0600 原生数据库 KEY=VALUE 配置文件")
    parser.add_argument("--allow-initialize", action="store_true",
                        help="仅部署器已确认受管空数据目录时允许首次初始化")
    parser.add_argument("--reconfigure-advertised-host", action="store_true",
                        help="仅把受管单成员副本集从127.0.0.1公告地址迁移到配置内网地址")
    args = parser.parse_args(argv)
    try:
        values = read_config(args.config)
        if args.reconfigure_advertised_host:
            if args.allow_initialize:
                raise ValueError("公告地址迁移不能同时初始化数据库")
            result = reconfigure_advertised_host(values)
            print("原生 MongoDB 公告地址迁移完成：成员={host}:{port} 已变更={changed}".format(**result))
            return 0
        result = initialize(values, allow_initialize=args.allow_initialize)
    except (OSError, PyMongoError, RuntimeError, ValueError):
        print("原生 MongoDB 初始化失败；请检查服务状态、配置和既有数据库归属")
        return 1
    print("原生 MongoDB 初始化完成：副本集={replicaSet} 成员={host}:{port} 数据库={database} 新建={created}".format(**result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
