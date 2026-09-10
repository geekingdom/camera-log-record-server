"""既有 Docker 入口的跨机拓扑预检与配置生成回归。"""

import base64
import importlib.util
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "scripts"))
spec = importlib.util.spec_from_file_location("deploy_cluster", root / "scripts" / "deploy_cluster.py")
cluster = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(cluster)


def _key():
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def _platform(password="p@:/?#$!"):
    return {
        "DEPLOY_TOPOLOGY": "multi-host", "DATABASE_HOST": "10.20.0.10", "DATABASE_BIND_IP": "10.20.0.10",
        "MONGO_PORT_1": "27017", "MONGO_PORT_2": "27018", "MONGO_PORT_3": "27019",
        "MONGO_ROOT_USERNAME": "camera_admin", "MONGO_ROOT_PASSWORD": password,
        "MONGO_REPLICA_KEY": "aGVsbG8tcmVwbGljYS1rZXk=", "DATABASE_NAME": "camera_logs",
        "ENCRYPTION_KEY": _key(), "BOOTSTRAP_TOKEN": "bootstrap", "INTERNAL_TOKEN": "internal",
        "API_BIND_IP": "10.20.0.10", "API_PORT": "8000", "BACKEND_UPSTREAM": "http://10.20.0.10:8000",
        "NODE_ID": "collector-a", "NODE_URL": "http://10.20.0.10:8001", "NODE_PORT": "8001",
        "HOST_LOG_ROOT": "/srv/camera-logs/a",
    }


def _write(path, values):
    path.write_text("\n".join(f"{key}={value!r}" for key, value in values.items()) + "\n", encoding="utf-8")


def test_platform_multihost_generates_uri_with_encoded_special_password(tmp_path):
    environment = tmp_path / "platform.env"
    _write(environment, _platform())

    values = cluster.prepare_platform_environment(environment)

    assert values["MONGO_URI"] == (
        "mongodb://camera_admin:p%40%3A%2F%3F%23%24%21@10.20.0.10:27017,10.20.0.10:27018,"
        "10.20.0.10:27019/camera_logs?replicaSet=rs0&authSource=admin"
    )
    assert "MONGO_URI=\"mongodb://" in environment.read_text(encoding="utf-8")


def test_authenticated_single_host_generates_internal_uri_with_encoded_password(tmp_path):
    """认证单机将管理员密码转换为仅 Compose 内部使用的安全 URI。"""
    environment = tmp_path / "platform.env"
    _write(environment, _platform())

    values = cluster.prepare_authenticated_single_host_environment(environment)

    assert values["COMPOSE_MONGO_URI"] == (
        "mongodb://camera_admin:p%40%3A%2F%3F%23%24%21@mongo1:27017,mongo2:27017,"
        "mongo3:27017/camera_logs?replicaSet=rs0&authSource=admin"
    )
    assert "p@:/?#$!" not in environment.read_text(encoding="utf-8").split("COMPOSE_MONGO_URI=", 1)[1]


def test_worker_multihost_accepts_external_single_member_and_literal_dollar(tmp_path):
    environment = tmp_path / "worker.env"
    values = _platform("kept$literal") | {
        "MONGO_URI": "mongodb://camera_admin:kept%24literal@10.20.0.10:27017/camera_logs?replicaSet=rs0&authSource=admin",
        "NODE_ID": "collector-b", "NODE_URL": "http://10.20.0.20:8001", "HOST_LOG_ROOT": "/srv/camera-logs/b",
    }
    _write(environment, values)

    assert cluster.validate_worker_environment(environment)["MONGO_ROOT_PASSWORD"] == "kept$literal"


def test_worker_multihost_rejects_database_name_different_from_uri(tmp_path):
    """Worker 的初始默认库名不能静默接入 A 的另一业务库。"""
    environment = tmp_path / "worker.env"
    values = _platform() | {
        "MONGO_URI": "mongodb://camera_admin:password@10.20.0.10:27017/custom_logs?replicaSet=rs0&authSource=admin",
        "NODE_ID": "collector-b", "NODE_URL": "http://10.20.0.20:8001", "HOST_LOG_ROOT": "/srv/camera-logs/b",
    }
    _write(environment, values)

    try:
        cluster.validate_worker_environment(environment)
    except ValueError as error:
        assert "DATABASE_NAME" in str(error)
    else:
        raise AssertionError("数据库名不一致必须被拒绝")


def test_double_quoted_dotenv_preserves_non_ascii_literals(tmp_path):
    """双引号 dotenv 值只展开有限转义，不能破坏 UTF-8 密码。"""
    environment = tmp_path / "environment.env"
    environment.write_text('PASSWORD="\u5bc6\u7801\\nline"\n', encoding="utf-8")

    assert cluster.read_environment(environment)["PASSWORD"] == "\u5bc6\u7801\nline"


def test_single_quoted_dotenv_preserves_dollar_variable_literal(tmp_path):
    """Compose 单引号密码不得把美元符后的名称当作宿主机变量展开。"""
    environment = tmp_path / "environment.env"
    environment.write_text("PASSWORD='keep$DOES_NOT_EXIST'\n", encoding="utf-8")

    assert cluster.read_environment(environment)["PASSWORD"] == "keep$DOES_NOT_EXIST"


def test_multihost_rejects_compose_member_and_loopback_worker(tmp_path):
    environment = tmp_path / "worker.env"
    values = _platform() | {
        "MONGO_URI": "mongodb://camera_admin:password@mongo1:27017/camera_logs?replicaSet=rs0&authSource=admin",
        "NODE_URL": "http://127.0.0.1:8001",
    }
    _write(environment, values)

    try:
        cluster.validate_worker_environment(environment)
    except ValueError as error:
        assert "Compose" in str(error)
    else:
        raise AssertionError("Compose 私网成员必须被拒绝")


def test_existing_entrypoints_expose_multihost_without_public_role_script():
    all_script = (root / "deploy-all.sh").read_text(encoding="utf-8")
    worker_script = (root / "deploy-worker.sh").read_text(encoding="utf-8")
    shared = (root / "deploy.sh").read_text(encoding="utf-8")

    assert "deploy.sh" in all_script and "deploy.sh" in worker_script
    assert "--multi-host" in shared and "deploy_cluster.py\" platform" in shared
    assert not (root / "deploy-cluster.sh").exists()


def test_existing_entrypoints_create_marked_config_once_and_expose_help(tmp_path):
    """用户只需两个既有脚本；首次初始化标记拓扑，重复操作不覆盖密钥。"""
    platform = tmp_path / "platform-a.env"
    worker = tmp_path / "worker-b.env"
    first = subprocess.run(["bash", str(root / "deploy-all.sh"), "--multi-host", "--env-file", str(platform), "--init"], text=True, capture_output=True, check=False)
    second = subprocess.run(["bash", str(root / "deploy-all.sh"), "--multi-host", "--env-file", str(platform), "--init"], text=True, capture_output=True, check=False)
    worker_first = subprocess.run(["bash", str(root / "deploy-worker.sh"), "--multi-host", "--env-file", str(worker), "--init"], text=True, capture_output=True, check=False)
    help_result = subprocess.run(["bash", str(root / "deploy-all.sh"), "--help"], text=True, capture_output=True, check=False)

    assert first.returncode == worker_first.returncode == help_result.returncode == 0
    assert second.returncode != 0
    assert "DEPLOY_TOPOLOGY=multi-host" in platform.read_text(encoding="utf-8")
    assert "MONGO_ROOT_PASSWORD=" in platform.read_text(encoding="utf-8")
    assert "DEPLOY_AUTHENTICATION" not in platform.read_text(encoding="utf-8")
    assert "DEPLOY_TOPOLOGY=multi-host" in worker.read_text(encoding="utf-8")
    assert "--multi-host" in help_result.stdout


def test_compose_config_keeps_special_database_password_literal(tmp_path):
    """Compose 展开必须让 Mongo 收到原密码，URI 中只能出现其URL编码形式。"""
    environment = tmp_path / "platform.env"
    _write(environment, _platform())
    values = cluster.prepare_platform_environment(environment)
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(environment), "--file", str(root / "deploy" / "database.yml"), "config", "--format", "json"],
        cwd=root, env=os.environ | {"DEPLOY_ENV_FILE": str(environment)}, text=True, capture_output=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    # Compose v5 的 config JSON 会将美元符重新转义为 $$；env-file 原值仍是单个 $。
    assert config["services"]["mongo1"]["environment"]["MONGO_INITDB_ROOT_PASSWORD"] == "p@:/?#$$!"
    environment_result = subprocess.run(
        ["docker", "compose", "--env-file", str(environment), "--file", str(root / "deploy" / "database.yml"), "config", "--environment"],
        cwd=root, env=os.environ | {"DEPLOY_ENV_FILE": str(environment)}, text=True, capture_output=True, check=False,
    )
    assert environment_result.returncode == 0, environment_result.stderr
    assert "MONGO_ROOT_PASSWORD=p@:/?#$!" in environment_result.stdout
    assert "%24" in values["MONGO_URI"] and "p@:/?#$!" not in values["MONGO_URI"]
