"""独立部署组件的配置预检与 Compose 展开测试，不创建 Docker 容器或卷。"""

import base64
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "scripts"))
spec = importlib.util.spec_from_file_location("deploy_component", root / "scripts" / "deploy_component.py")
component = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(component)

verifier_spec = importlib.util.spec_from_file_location(
    "verify_component_deployment", root / "scripts" / "verify_component_deployment.py"
)
verifier = importlib.util.module_from_spec(verifier_spec)
assert verifier_spec and verifier_spec.loader
verifier_spec.loader.exec_module(verifier)


def _key():
    """生成部署预检所需但不含真实环境凭据的有效 Fernet 密钥。"""
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()


def _services(component_name, values, *, directories=("/srv/one", "/srv/two", "/srv/three")):
    """构造与 Compose JSON 相同的最小服务投影，隔离验证输入而不解析 YAML。"""
    target = {"frontend": "frontend", "backend": "api", "worker": "worker", "database": "mongo1"}[component_name]
    services = {target: {"environment": values, "volumes": []}}
    if component_name == "database":
        services = {
            f"mongo{index}": {"environment": {"MONGO_PORT": str(27116 + index), "DATABASE_HOST": "127.0.0.1",
                                                 "MONGO_INITDB_ROOT_USERNAME": "camera_admin",
                                                 "MONGO_INITDB_ROOT_PASSWORD": "temporary"},
                               "volumes": [{"type": "bind", "source": directory, "target": "/data/db"}]}
            for index, directory in enumerate(directories, 1)
        }
        services["key-init"] = {"environment": {"MONGO_REPLICA_KEY": base64.b64encode(b"component-test-key").decode()}}
    return {"services": services}


def _runtime_values():
    """返回后端和采集节点必须共享的一组非生产测试凭据。"""
    return {"MONGO_URI": "mongodb://camera_admin:encoded@127.0.0.1:27117,127.0.0.1:27118,127.0.0.1:27119/camera_logs?replicaSet=rs0",
            "ENCRYPTION_KEY": _key(), "BOOTSTRAP_TOKEN": "bootstrap", "INTERNAL_TOKEN": "internal"}


@pytest.mark.parametrize("name,values", [
    ("frontend", {"BACKEND_UPSTREAM": "http://172.17.0.1:18000"}),
    ("backend", _runtime_values() | {"API_PORT": "18000"}),
    ("worker", _runtime_values() | {"NODE_ID": "component-test", "NODE_URL": "http://127.0.0.1:18001", "NODE_PORT": "18001"}),
    ("database", {}),
])
def test_component_validation_accepts_complete_isolated_configuration(name, values):
    """每个独立组件仅在它自身的完整配置满足时通过预检。"""
    resolved = component.validate(name, _services(name, values))
    if name == "database":
        assert resolved["MONGO_PORT"] == "27117" and resolved["DATABASE_HOST"] == "127.0.0.1"
    else:
        assert resolved == values


@pytest.mark.parametrize("name,values", [
    ("frontend", {"BACKEND_UPSTREAM": "http://user:password@host:18000/path"}),
    ("backend", _runtime_values() | {"MONGO_URI": "https://not-mongodb"}),
    ("worker", _runtime_values() | {"NODE_ID": "node", "NODE_URL": "https://host/path", "NODE_PORT": "18001"}),
])
def test_component_validation_rejects_unsafe_or_wrong_upstream_values(name, values):
    """前端代理和节点公布地址不能接受路径、凭据或非 MongoDB 连接串。"""
    with pytest.raises(ValueError):
        component.validate(name, _services(name, values))


def test_database_validation_requires_distinct_safe_data_directories_and_ports():
    """三成员数据库不可复用目录或端口，也不能把根目录作为数据目录。"""
    with pytest.raises(ValueError, match="不同"):
        component.validate("database", _services("database", {}, directories=("/srv/one", "/srv/one", "/srv/three")))
    unsafe = _services("database", {}, directories=("/", "/srv/two", "/srv/three"))
    with pytest.raises(ValueError, match="非根目录"):
        component.validate("database", unsafe)
    duplicate_port = _services("database", {})
    duplicate_port["services"]["mongo2"]["environment"]["MONGO_PORT"] = "27117"
    with pytest.raises(ValueError, match="互不相同"):
        component.validate("database", duplicate_port)


def test_database_compose_prepares_each_empty_volume_off_host_network_before_member_start():
    """首次认证必须在隔离网络完成，host 网络成员只在对应作业成功后才可启动。"""
    source = (root / "deploy" / "database.yml").read_text(encoding="utf-8")
    headers = list(re.finditer(r"^  ([A-Za-z0-9-]+):$", source, re.MULTILINE))
    blocks = {
        header.group(1): source[header.end():headers[position + 1].start() if position + 1 < len(headers) else len(source)]
        for position, header in enumerate(headers)
    }
    for index in (1, 2, 3):
        job = f"mongo{index}-user-init"
        assert "network_mode: host" not in blocks[job]
        assert f'"${{MONGO_DATA_{index}:-mongo{index}-data}}:/data/db"' in blocks[job]
        assert f"{job}: {{condition: service_completed_successfully}}" in blocks[f"mongo{index}"]


def test_database_user_initializer_skips_nonempty_volumes_before_starting_mongod_and_hides_password():
    """重复部署不能抢占活动数据锁，凭据也不能出现在 Shell 输出或命令参数中。"""
    source = (root / "deploy" / "mongo-host-user-init.sh").read_text(encoding="utf-8")
    skip = source.index("find /data/db")
    start = source.index("gosu mongodb mongod")
    assert skip < start
    assert "process.env.MONGO_INITDB_ROOT_PASSWORD" in source
    assert "echo \"$MONGO_INITDB_ROOT_PASSWORD\"" not in source
    assert "--password" not in source


def test_database_health_requires_all_one_shot_jobs_to_exit_successfully(monkeypatch):
    """预初始化或副本集初始化缺失、失败时，独立部署不可误报健康。"""
    calls = []

    def fake_run(command, deadline):
        calls.append(command)
        if command[-2:] == ["-q", "mongo2-user-init"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "container-id\n", "")

    monkeypatch.setattr(component, "_run", fake_run)
    assert not component.completed_successfully(["docker", "compose"], "mongo2-user-init", time.monotonic() + 1)
    assert not any(command[:2] == ["docker", "inspect"] for command in calls)


def test_backend_and_worker_require_the_same_shared_credentials():
    """独立 Worker 不生成自身密钥，测试配置显式复用后端的数据库和三项认证材料。"""
    shared = _runtime_values()
    backend = component.validate("backend", _services("backend", shared | {"API_PORT": "18000"}))
    worker = component.validate("worker", _services("worker", shared | {
        "NODE_ID": "component-test", "NODE_URL": "http://127.0.0.1:18001", "NODE_PORT": "18001",
    }))
    assert {key: backend[key] for key in shared} == {key: worker[key] for key in shared}


def test_component_wrappers_delegate_to_only_their_own_component():
    """四个入口必须固定组件参数；验证器据此使用不同随机 Compose 项目名避免服务碰撞。"""
    for name in ("database", "backend", "worker", "frontend"):
        script = (root / f"deploy-{name}.sh").read_text(encoding="utf-8")
        assert f'--component {name}' in script
    verifier_source = (root / "scripts" / "verify_component_deployment.py").read_text(encoding="utf-8")
    assert 'project_base = f"camera-components-{identity}"' in verifier_source
    assert 'projects = {name: f"{project_base}-{name}" for name in COMPONENTS}' in verifier_source
    assert "deploy_component(name, project_base" in verifier_source


def test_verifier_passes_the_same_temporary_environment_to_wrappers_and_compose(monkeypatch, tmp_path):
    """直接 Compose 重启和正式入口启动都必须向 YAML 的 env_file 注入同一临时文件。"""
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    environment = tmp_path / ".backend.env"
    environment.write_text("API_PORT=18000\n", encoding="utf-8")
    monkeypatch.setattr(verifier.subprocess, "run", fake_run)
    verifier.deploy_component("backend", "component-test", environment, 1)
    verifier.restart_component("component-test-backend", "backend", environment)

    wrapper_call, compose_call = calls
    assert wrapper_call[1]["env"]["COMPOSE_PROJECT_NAME"] == "component-test"
    assert wrapper_call[1]["env"]["DEPLOY_ENV_FILE"] == str(environment)
    assert compose_call[0][:2] == ["docker", "compose"]
    assert compose_call[1]["env"]["DEPLOY_ENV_FILE"] == str(environment)


def test_verifier_worker_restart_probe_checks_local_health_and_mongo_heartbeat(monkeypatch, tmp_path):
    """重启后的独立节点必须在自身容器中同时证明 HTTP 健康和最新 MongoDB 心跳。"""
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    environment = tmp_path / ".worker.env"
    environment.write_text("NODE_PORT=18001\n", encoding="utf-8")
    monkeypatch.setattr(verifier.subprocess, "run", fake_run)

    assert verifier.worker_ready("component-test-worker", environment, deadline=time.monotonic() + 1)

    command, kwargs = calls[0]
    assert command[:2] == ["docker", "compose"]
    assert "--project-name" in command and command[command.index("--project-name") + 1] == "component-test-worker"
    assert command[-6:-1] == ["exec", "-T", "worker", "python", "-c"]
    assert 'nodes.find_one({"id": settings.node_id})' in command[-1]
    assert '"/health"' in command[-1]
    assert kwargs["env"]["DEPLOY_ENV_FILE"] == str(environment)


def test_verifier_cleanup_checks_labeled_objects_and_removes_root_owned_bind_content(monkeypatch, tmp_path):
    """清理只检查随机项目 label，并用一次性容器清空可能由 Mongo 创建的挂载内容。"""
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    environment = tmp_path / ".database.env"
    environment.write_text("MONGO_PORT_1=27117\n", encoding="utf-8")
    data_root = Path(tempfile.mkdtemp(prefix="camera-component-deploy-"))
    (data_root / "mongo1").mkdir()
    (data_root / "mongo1" / "state").write_text("temporary", encoding="utf-8")
    monkeypatch.setattr(verifier.subprocess, "run", fake_run)

    verifier.cleanup({"database": "component-test-database"}, {"database": environment}, data_root)

    commands = [command for command, _ in calls]
    assert any(command[:2] == ["docker", "compose"] and command[-3:] == ["down", "--volumes", "--remove-orphans"]
               for command in commands)
    assert ["docker", "ps", "--all", "--quiet", "--filter",
            "label=com.docker.compose.project=component-test-database"] in commands
    assert ["docker", "volume", "ls", "--quiet", "--filter",
            "label=com.docker.compose.project=component-test-database"] in commands
    assert any(command[:3] == ["docker", "run", "--rm"] and "alpine:3.21" in command for command in commands)
    assert not data_root.exists()


def _component_deployment_copy(tmp_path):
    """复制独立入口所需的最小文件集，让项目名规则在真实 Bash 进程中执行。"""
    app = tmp_path / "component-app"
    (app / "deploy").mkdir(parents=True)
    (app / "scripts").mkdir()
    shutil.copy2(root / "deploy.sh", app / "deploy.sh")
    for name in ("deploy_env.py", "deploy_docker.sh"):
        shutil.copy2(root / "scripts" / name, app / "scripts" / name)
    for name in ("backend.yml", "worker.yml", "database.yml", "frontend.yml"):
        (app / "deploy" / name).write_text("services: {}\n", encoding="utf-8")
    (app / "scripts" / "deploy_component.py").write_text(
        "import os, sys\n"
        "with open(os.environ['MOCK_COMPONENT_LOG'], 'a', encoding='utf-8') as output:\n"
        "    output.write(' '.join(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    return app


def _component_mock_bin(tmp_path):
    """提供满足入口预检的 Docker 替身，只记录而不访问本机 daemon。"""
    directory = tmp_path / "component-bin"
    directory.mkdir()
    (directory / "uname").write_text("#!/bin/sh\necho Linux\n", encoding="utf-8")
    (directory / "docker").write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = compose ] || [ \"$1\" = info ] || [ \"$1\" = volume ]; then exit 0; fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    (directory / "systemctl").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (directory / "sudo").write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
    for path in directory.iterdir():
        path.chmod(0o755)
    return directory


def test_component_entrypoints_append_distinct_suffixes_to_one_explicit_project_base(tmp_path):
    """同一 COMPOSE_PROJECT_NAME 启动后端和 Worker 时，入口必须生成互不冲突的项目名。"""
    app, bin_dir = _component_deployment_copy(tmp_path), _component_mock_bin(tmp_path)
    backend_env, worker_env = app / ".backend.env", app / ".worker.env"
    backend_env.write_text("API_PORT=18000\n", encoding="utf-8")
    worker_env.write_text("NODE_PORT=18001\n", encoding="utf-8")
    log = app / "components.log"
    environment = os.environ | {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "COMPOSE_PROJECT_NAME": "shared-component-test",
        "MOCK_COMPONENT_LOG": str(log),
    }
    for name, environment_file in (("backend", backend_env), ("worker", worker_env)):
        result = subprocess.run(["bash", str(app / "deploy.sh"), "--component", name,
                                 "--env-file", str(environment_file)], cwd=app, env=environment, text=True,
                                capture_output=True, check=False)
        assert result.returncode == 0, result.stderr

    commands = log.read_text(encoding="utf-8").splitlines()
    assert any("--component backend --project shared-component-test-backend" in command for command in commands)
    assert any("--component worker --project shared-component-test-worker" in command for command in commands)


@pytest.mark.skipif(shutil.which("docker") is None, reason="需要 Docker Compose 仅作配置展开")
@pytest.mark.parametrize("name", ["database", "backend", "worker", "frontend"])
def test_real_compose_config_expands_each_component_without_printing_test_secrets(tmp_path, name):
    """真实 Docker Compose 只展开组件配置；测试捕获输出且不把密钥写入断言消息。"""
    version = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True, check=False)
    if version.returncode:
        pytest.skip("Docker Compose 不可用")
    secret = secrets.token_urlsafe(24)
    values = _runtime_values() | {"BOOTSTRAP_TOKEN": secret, "INTERNAL_TOKEN": secret + "-internal"}
    values.update({
        "DATABASE_HOST": "127.0.0.1", "DATABASE_BIND_IP": "0.0.0.0", "MONGO_PORT_1": "27117",
        "MONGO_PORT_2": "27118", "MONGO_PORT_3": "27119", "MONGO_ROOT_USERNAME": "camera_admin",
        "MONGO_ROOT_PASSWORD": "encoded", "MONGO_REPLICA_KEY": base64.b64encode(secrets.token_bytes(48)).decode(),
        "MONGO_DATA_1": str(tmp_path / "mongo1"), "MONGO_DATA_2": str(tmp_path / "mongo2"),
        "MONGO_DATA_3": str(tmp_path / "mongo3"), "API_BIND_IP": "0.0.0.0", "API_PORT": "18000",
        "API_DATA_ROOT": str(tmp_path / "api"), "FORWARDED_ALLOW_IPS": "172.17.0.1",
        "NODE_ID": "component-test", "NODE_URL": "http://127.0.0.1:18001", "NODE_PORT": "18001",
        "HOST_LOG_ROOT": str(tmp_path / "worker"), "BACKEND_UPSTREAM": "http://172.17.0.1:18000",
        "FRONTEND_PORT": "15174", "DEPLOY_ENV_FILE": str(tmp_path / ".env"),
    })
    env_file = tmp_path / ".env"
    env_file.write_text("".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8")
    result = subprocess.run(["docker", "compose", "--env-file", str(env_file), "--file", str(root / "deploy" / f"{name}.yml"),
                             "config", "--format", "json"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert json.loads(result.stdout)["services"]
    assert secret not in result.stderr


@pytest.mark.parametrize("address", ["http://0.0.0.0:8001", "http://[::]:8001", "http://host:0", "http://host:65536"])
def test_endpoint_rejects_wildcard_targets_and_invalid_ports(address):
    """公布地址必须可用作连接目标，不能误用监听地址或无效端口。"""
    with pytest.raises(ValueError):
        component.endpoint(address, "NODE_URL")


@pytest.mark.parametrize("address", ["http://node.example:8001", "http://[::1]:8001", "https://node.example"])
def test_endpoint_accepts_dns_ipv6_and_https(address):
    """允许 DNS、明确 IPv6 地址与 HTTPS 默认端口。"""
    component.endpoint(address, "NODE_URL")
