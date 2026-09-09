"""原生 MongoDB 初始化器的副本集、认证和未知数据保护契约。"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest
from pymongo.errors import OperationFailure

ROOT = Path(__file__).resolve().parents[1]


def _module(monkeypatch):
    """加载尚未与部署入口合并的脚本，并为配置读取接口提供最小替身。"""
    config = types.ModuleType("native_config")
    config.read_config = lambda _path: {}
    monkeypatch.setitem(sys.modules, "native_config", config)
    spec = importlib.util.spec_from_file_location("native_database", ROOT / "scripts" / "native_database.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class _Server:
    """记录管理员命令，模拟 localhost exception 和已有认证数据库。"""

    def __init__(self, *, databases=(), replica=None, administrator=None):
        self.databases = list(databases)
        self.replica = replica
        self.administrator = administrator
        self.commands = []

    def client(self, uri, **_kwargs):
        return _Client(self, uri)


class _Client:
    """仅实现初始化器所需的 PyMongo 同步客户端表面。"""

    def __init__(self, server, uri):
        self.server, self.uri = server, uri
        self.admin = _Admin(server, uri)

    def list_database_names(self):
        return self.server.databases

    def close(self):
        return None


class _Admin:
    """根据 URI 是否携带正确管理员密码决定认证结果。"""

    def __init__(self, server, uri):
        self.server, self.uri = server, uri

    def command(self, command):
        name = next(iter(command)) if isinstance(command, dict) else command
        self.server.commands.append((name, command))
        authenticated = self.server.administrator and all(
            item in self.uri for item in self.server.administrator[:2]
        )
        if name == "connectionStatus":
            if authenticated:
                return {"authInfo": {"authenticatedUsers": [{"user": self.server.administrator[0]}]}}
            raise OperationFailure("Authentication failed", code=18)
        if name == "replSetGetConfig":
            if self.server.replica is None:
                raise OperationFailure("not initialized", code=94)
            return {"config": self.server.replica}
        if name == "replSetInitiate":
            if self.server.replica is not None:
                raise OperationFailure("already initialized", code=23)
            self.server.replica = command["replSetInitiate"]
            return {"ok": 1}
        if name == "hello":
            return {"isWritablePrimary": True}
        if name == "createUser":
            if self.server.administrator is not None:
                raise OperationFailure("user already exists", code=51003)
            self.server.administrator = (command["createUser"], command["pwd"])
            return {"ok": 1}
        raise AssertionError(f"unexpected command: {command}")


def _values(**changes):
    """给出单成员部署的完整最小配置。"""
    return {
        "MONGO_ADMIN_USER": "camera_admin",
        "MONGO_ADMIN_PASSWORD": "correct-password",
        "DATABASE_NAME": "camera_logs",
    } | changes


def test_first_run_initializes_advertised_single_member_and_root_administrator(monkeypatch):
    """空实例只经 localhost exception 初始化副本集并创建一个 root 管理员。"""
    module, server = _module(monkeypatch), _Server()

    result = module.initialize(_values(MONGO_ADVERTISED_HOST="mongo.example"), server.client,
                               allow_initialize=True, timeout_seconds=0)

    assert result == {"replicaSet": "rs0", "host": "mongo.example", "port": 27017,
                      "database": "camera_logs", "created": True}
    assert server.replica == {"_id": "rs0", "members": [{"_id": 0, "host": "mongo.example:27017"}]}
    assert server.administrator == ("camera_admin", "correct-password")
    created = next(command for name, command in server.commands if name == "createUser")
    assert created["roles"] == ["root"]


def test_repeat_uses_known_credentials_and_does_not_reconfigure_or_replace_user(monkeypatch):
    """已有受管实例必须先认证，再仅核验副本集和管理员身份。"""
    module, server = _module(monkeypatch), _Server()
    module.initialize(_values(), server.client, allow_initialize=True, timeout_seconds=0)
    server.commands.clear()

    result = module.initialize(_values(), server.client, timeout_seconds=0)

    assert result["created"] is False
    assert [name for name, _ in server.commands] == ["connectionStatus", "replSetGetConfig"]


def test_wrong_existing_credentials_never_attempts_initialization_or_user_creation(monkeypatch):
    """认证失败时不能把已有实例误当空库，也不能覆盖管理员密码。"""
    module, server = _module(monkeypatch), _Server()
    module.initialize(_values(), server.client, allow_initialize=True, timeout_seconds=0)
    server.commands.clear()

    with pytest.raises(RuntimeError, match="仅受管空目录") as error:
        module.initialize(_values(MONGO_ADMIN_PASSWORD="wrong-password"), server.client, timeout_seconds=0)

    assert "wrong-password" not in str(error.value)
    assert [name for name, _ in server.commands] == ["connectionStatus"]
    assert server.administrator == ("camera_admin", "correct-password")


def test_unknown_existing_database_refuses_localhost_exception_privilege_escalation(monkeypatch):
    """首次管理员创建前发现业务库数据时拒绝写入，不能接管未知数据库。"""
    module, server = _module(monkeypatch), _Server(databases=["camera_logs"])

    with pytest.raises(RuntimeError, match="未知业务数据库"):
        module.initialize(_values(), server.client, allow_initialize=True, timeout_seconds=0)

    assert server.replica is not None and server.administrator is None
    assert [name for name, _ in server.commands] == ["connectionStatus", "replSetInitiate", "hello"]


def test_existing_different_replica_set_is_rejected_without_user_mutation(monkeypatch):
    """已初始化但不可用既有凭据的实例不允许以初始化流程重配或创建用户。"""
    module = _module(monkeypatch)
    server = _Server(replica={"_id": "other", "members": [{"_id": 0, "host": "127.0.0.1:27017"}]})

    with pytest.raises(RuntimeError, match="已有副本集"):
        module.initialize(_values(), server.client, allow_initialize=True, timeout_seconds=0)

    assert server.administrator is None
    assert [name for name, _ in server.commands] == ["connectionStatus", "replSetInitiate", "replSetGetConfig"]


def test_managed_initialize_recovers_matching_replica_set_before_first_user(monkeypatch):
    """受管 marker 保留时，同配置副本集的首次 createUser 中断可安全恢复。"""
    module = _module(monkeypatch)
    server = _Server(replica={"_id": "rs0", "members": [{"_id": 0, "host": "127.0.0.1:27017"}]})

    result = module.initialize(_values(), server.client, allow_initialize=True, timeout_seconds=0)

    assert result["created"] is True
    assert server.administrator == ("camera_admin", "correct-password")
    assert [name for name, _ in server.commands] == [
        "connectionStatus", "replSetInitiate", "replSetGetConfig", "hello", "createUser",
    ]


def test_ipv6_advertised_host_is_not_supported_in_first_native_release(monkeypatch):
    """初版不接受 IPv6 字面量，避免生成未覆盖的副本集成员地址格式。"""
    module = _module(monkeypatch)

    with pytest.raises(ValueError, match="IPv4 或 DNS"):
        module.initialize(_values(MONGO_ADVERTISED_HOST="::1"), _Server().client, timeout_seconds=0)


def test_cli_failure_never_prints_configured_password(monkeypatch, capsys, tmp_path):
    """命令行只报告固定中文错误，不把驱动异常或配置密码回显到终端。"""
    module = _module(monkeypatch)
    secret = "dont-print-this-password"
    monkeypatch.setattr(module, "read_config", lambda _path: _values(MONGO_ADMIN_PASSWORD=secret))

    def fail(_values, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(module, "initialize", fail)

    assert module.main(["--config", str(tmp_path / "database.env")]) == 1
    assert secret not in capsys.readouterr().out
