"""验证原生部署编排只发布请求组件，且不需要真实 systemd 主机。"""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import deploy_native
import pytest
from native_config import defaults
from native_units import render


def publish_migration_contract(values, previous, root):
    """写入首次部署会生成的受保护文件，用于验证公告地址迁移的预检边界。"""
    etc = root / "etc"
    etc.mkdir()
    files = render(previous, Path(__file__).resolve().parents[1])
    for name in ("api.env", "mongod.conf", "mongo.key"):
        (etc / name).write_text(files["backend" if name == "api.env" else "database"][name])


@pytest.mark.parametrize("key", ["INTERNAL_TOKEN", "MONGO_PORT", "API_PORT", "NODE_URL"])
def test_shared_contract_change_rejected_before_stopping(key, tmp_path):
    values = defaults() | {"INSTALL_ROOT": str(tmp_path)}
    known = {name: values[name] for name in ("SERVICE_USER", "DATA_ROOT", "MONGO_DATA_ROOT", "LOG_ROOT", "API_LOG_ROOT")}
    known.update(encryptionKeyHash=hashlib.sha256(values["ENCRYPTION_KEY"].encode()).hexdigest(),
                 contractHash=deploy_native.contract_hash(values))
    (tmp_path / ".native-managed.json").write_text(json.dumps(known))
    with pytest.raises(ValueError, match="跨组件"):
        deploy_native.preflight(values | {key: "changed"}, ("backend",))


def test_single_component_deploy_keeps_other_services_untouched(monkeypatch, tmp_path):
    """后端独立更新只准备其虚拟环境和 unit，不停止或重写其它组件。"""
    values = defaults() | {"INSTALL_PACKAGES": "false"}
    calls = []
    account = SimpleNamespace(pw_uid=10001, pw_gid=10001)
    files = {name: {f"{name}.service": name} for name in deploy_native.COMPONENTS}

    monkeypatch.setattr(deploy_native, "preflight", lambda actual, selected: calls.append(("preflight", selected)))
    monkeypatch.setattr(deploy_native, "prepare_directories", lambda actual: account)
    monkeypatch.setattr(deploy_native, "python_runtime", lambda actual: Path("/python312"))
    monkeypatch.setattr(deploy_native, "render", lambda actual, source: files)
    monkeypatch.setattr(deploy_native, "prepare_venv",
                        lambda actual, source, component, python: calls.append(("venv", component, python)) or Path("/venv/python"))
    monkeypatch.setattr(deploy_native, "publish",
                        lambda actual, component, rendered, owner: calls.append(("publish", component, rendered)))
    monkeypatch.setattr(deploy_native, "wait_http", lambda url, **kwargs: calls.append(("health", url)))
    monkeypatch.setattr(deploy_native, "build_frontend", lambda *_args: (_ for _ in ()).throw(AssertionError("不应构建前端")))
    monkeypatch.setattr(deploy_native, "run", lambda command, **kwargs: (_ for _ in ()).throw(AssertionError(command)))

    deploy_native.deploy(values, "backend", tmp_path / "native.env", tmp_path)

    assert calls[0] == ("preflight", ("backend",))
    assert ("venv", "backend", Path("/python312")) in calls
    assert ("publish", "backend", files["backend"]) in calls
    assert all(not (call[0] == "publish" and call[1] != "backend") for call in calls)
    assert ("health", "http://127.0.0.1:18080/health") in calls


def test_repeated_init_does_not_replace_existing_config(monkeypatch, tmp_path):
    """已有配置再次 --init 不生成密钥，也不触发安装或服务操作。"""
    config = tmp_path / "native.env"
    config.write_text("INSTALL_ROOT=/opt/example\n", encoding="utf-8")
    before = config.read_bytes()

    monkeypatch.setattr(deploy_native, "create_config",
                        lambda *_args: (_ for _ in ()).throw(AssertionError("已有配置不得重新生成")))

    assert deploy_native.main(["all", "--config", str(config), "--init"]) == 0
    assert config.read_bytes() == before


def test_first_managed_database_deploy_explicitly_allows_initialization(monkeypatch, tmp_path):
    """预检确认空受管目录后，部署器才可让 Mongo 使用 localhost exception。"""
    values = defaults() | {"INSTALL_PACKAGES": "false", "MONGO_DATA_ROOT": str(tmp_path / "mongo")}
    account = SimpleNamespace(pw_uid=10001, pw_gid=10001)
    commands = []
    (tmp_path / "mongo").mkdir()
    (tmp_path / "etc").mkdir()
    values["INSTALL_ROOT"] = str(tmp_path)
    config = tmp_path / "native.env"
    config.write_text("CONFIG=first\n", encoding="utf-8")

    monkeypatch.setattr(deploy_native, "preflight", lambda *_args: None)
    monkeypatch.setattr(deploy_native, "prepare_directories", lambda _values: account)
    monkeypatch.setattr(deploy_native, "python_runtime", lambda _values: Path("/python312"))
    monkeypatch.setattr(deploy_native, "render", lambda *_args: {"database": {"mongo.service": "unit"}})
    monkeypatch.setattr(deploy_native, "prepare_venv", lambda *_args: Path("/venv/database/bin/python"))
    monkeypatch.setattr(deploy_native, "publish", lambda *_args: None)
    monkeypatch.setattr(deploy_native, "run", lambda command, **_kwargs: commands.append(command))

    deploy_native.deploy(values, "database", config, tmp_path)

    command = next(command for command in commands if any(Path(item).name == "native_database.py" for item in command))
    assert command[-1] == "--allow-initialize"


def test_database_advertised_host_migration_is_explicit_and_refreshes_contract(monkeypatch, tmp_path):
    """已有受管平台只在显式数据库迁移后更新合同摘要，不影响其它组件。"""
    previous = defaults() | {"INSTALL_PACKAGES": "false", "INSTALL_ROOT": str(tmp_path), "MONGO_DATA_ROOT": str(tmp_path / "mongo")}
    values = previous | {"MONGO_ADVERTISED_HOST": "10.42.0.10",
                         "MONGO_BIND_IP": "127.0.0.1,10.42.0.10"}
    values["MONGO_URI"] = previous["MONGO_URI"].replace("@127.0.0.1:", "@10.42.0.10:")
    account = SimpleNamespace(pw_uid=10001, pw_gid=10001)
    marker = {name: values[name] for name in ("SERVICE_USER", "DATA_ROOT", "MONGO_DATA_ROOT", "LOG_ROOT", "API_LOG_ROOT")}
    marker.update(encryptionKeyHash=hashlib.sha256(values["ENCRYPTION_KEY"].encode()).hexdigest(), contractHash="old")
    (tmp_path / ".native-managed.json").write_text(json.dumps(marker))
    (tmp_path / "mongo").mkdir()
    publish_migration_contract(values, previous, tmp_path)
    (tmp_path / "native.env").write_text("CONFIG=existing\n")
    commands = []
    monkeypatch.setattr(deploy_native, "prepare_directories", lambda _values: account)
    monkeypatch.setattr(deploy_native, "python_runtime", lambda _values: Path("/python312"))
    monkeypatch.setattr(deploy_native, "render", lambda *_args: {"database": {"mongo.service": "unit"}})
    monkeypatch.setattr(deploy_native, "prepare_venv", lambda *_args: Path("/venv/database/bin/python"))
    monkeypatch.setattr(deploy_native, "publish", lambda *_args: None)
    monkeypatch.setattr(deploy_native, "run", lambda command, **_kwargs: commands.append(command))

    deploy_native.deploy(values, "database", tmp_path / "native.env", tmp_path, reconfigure_mongo_advertised_host=True)

    command = next(command for command in commands if any(Path(item).name == "native_database.py" for item in command))
    assert command[-1] == "--reconfigure-advertised-host"
    assert json.loads((tmp_path / ".native-managed.json").read_text())["contractHash"] == deploy_native.contract_hash(values)


@pytest.mark.parametrize("key", ["INTERNAL_TOKEN", "BOOTSTRAP_TOKEN", "API_PORT", "NODE_URL", "MONGO_REPLICA_KEY"])
def test_advertised_host_migration_rejects_changed_protected_credentials(key, tmp_path):
    """显式迁移不能借合同摘要更新同时替换令牌、节点地址或Mongo成员密钥。"""
    previous = defaults() | {"INSTALL_ROOT": str(tmp_path), "MONGO_DATA_ROOT": str(tmp_path / "mongo")}
    values = previous | {"MONGO_ADVERTISED_HOST": "10.42.0.10", "MONGO_BIND_IP": "127.0.0.1,10.42.0.10"}
    values["MONGO_URI"] = previous["MONGO_URI"].replace("@127.0.0.1:", "@10.42.0.10:")
    values[key] = "changed" if key not in {"API_PORT", "MONGO_REPLICA_KEY"} else ("28017" if key == "API_PORT" else "aGVsbG8=")
    marker = {name: previous[name] for name in ("SERVICE_USER", "DATA_ROOT", "MONGO_DATA_ROOT", "LOG_ROOT", "API_LOG_ROOT")}
    marker.update(encryptionKeyHash=hashlib.sha256(previous["ENCRYPTION_KEY"].encode()).hexdigest(),
                  contractHash=deploy_native.contract_hash(previous))
    (tmp_path / ".native-managed.json").write_text(json.dumps(marker))
    publish_migration_contract(values, previous, tmp_path)

    with pytest.raises(ValueError, match="受保护配置|成员密钥"):
        deploy_native.preflight(values, ("database",), allow_mongo_advertised_host_migration=True)
