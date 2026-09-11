"""Linux 一键部署的环境保护、Compose 策略和健康检查命令测试。"""

import base64
import json
import os
import runpy
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest
from dotenv import dotenv_values

root = Path(__file__).resolve().parents[1]
environment = runpy.run_path(str(root / "scripts/deploy_env.py"))
health = runpy.run_path(str(root / "scripts/deploy_health.py"))
user_auth = runpy.run_path(str(root / "scripts/verify_user_auth.py"))


def test_deploy_environment_is_private_and_uses_distinct_random_credentials(tmp_path):
    """首次部署配置必须包含不同令牌、有效密钥和仅所有者可读权限。"""
    path = tmp_path / ".env"
    environment["create_environment"](path)
    values = dotenv_values(path)
    assert len(base64.urlsafe_b64decode(values["ENCRYPTION_KEY"])) == 32
    assert values["BOOTSTRAP_TOKEN"] != values["INTERNAL_TOKEN"]
    assert values["ADMIN_USERNAME"] == "admin"
    assert values["ADMIN_PASSWORD"] == "asdf!234"
    assert values["SESSION_SECONDS"] == "28800" and values["SESSION_COOKIE_SECURE"] == "false"
    assert values["COLLECTOR_NODE_ID"] == "compose-worker-1"
    assert values["COLLECTOR_NODE_URL"] == "http://worker:18081"
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_deploy_environment_refuses_to_replace_existing_keys(tmp_path):
    """已有环境文件时不得用新密钥覆盖，避免现有加密数据不可恢复。"""
    path = tmp_path / ".env"
    path.write_text("ENCRYPTION_KEY=preserve", encoding="utf-8")
    with pytest.raises(FileExistsError):
        environment["create_environment"](path)
    assert path.read_text(encoding="utf-8") == "ENCRYPTION_KEY=preserve"


def test_user_auth_smoke_uses_exact_ipv4_and_ipv6_source_networks():
    """隔离验收只能允许代理回显的单一来源地址，不能将来源扩大为整段网络。"""
    assert user_auth["_source_network"]("198.51.100.9") == "198.51.100.9/32"
    assert user_auth["_source_network"]("2001:db8:1::9") == "2001:db8:1::9/128"
    with pytest.raises(ValueError):
        user_auth["_source_network"]("not-an-ip")


def test_compose_declares_restart_log_rotation_and_frontend_healthcheck():
    """常驻服务应自动恢复、限制 Docker 日志，并让前端可被部署脚本检查。"""
    compose = (root / "deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert compose.count("restart: unless-stopped") == 6
    assert "driver: local" in compose and 'max-size: "10m"' in compose and 'max-file: "3"' in compose
    assert "frontend:" in compose and "wget -q -O /dev/null http://127.0.0.1/" in compose


def test_docker_build_supports_company_pip_default_and_external_ci_override():
    """生产镜像默认内网源，CI 必须能显式覆盖为公网 PyPI。"""
    for filename in ("api.Dockerfile", "worker.Dockerfile"):
        source = (root / "deploy" / filename).read_text(encoding="utf-8")
        assert "ARG PIP_INDEX_URL=http://af.hikvision.com.cn/" in source
        assert "ARG PIP_TRUSTED_HOST=af.hikvision.com.cn" in source
        assert '--index-url "$PIP_INDEX_URL"' in source
    compose = (root / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    assert "PIP_INDEX_URL: ${PIP_INDEX_URL:-http://af.hikvision.com.cn/" in compose
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "PIP_INDEX_URL: https://pypi.org/simple" in workflow
    assert 'PIP_INDEX_URL=https://pypi.org/simple' in workflow


def test_single_host_compose_uses_empty_volume_initializers_and_authenticated_clients():
    """完整单机部署默认认证，并在空卷阶段创建各成员的 root 用户。"""
    compose = (root / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")
    database = (root / "deploy" / "database.yml").read_text(encoding="utf-8")
    assert "key-init:" in compose and "mongo-key:" in compose
    for source in (compose, database):
        assert "cmp -s /keys/replica.key" in source
        assert "副本集成员认证密钥与已有持久化密钥不一致" in source
    for index in (1, 2, 3):
        assert f"mongo{index}-user-init:" in compose
        assert f"mongo{index}-user-init: {{condition: service_completed_successfully}}" in compose
        assert "MONGO_INITDB_ROOT_USERNAME: ${MONGO_ROOT_USERNAME:?" in compose
        assert "--keyFile" in compose
    assert "--authenticationDatabase admin" in compose
    assert "mongo-host-user-init.sh:/scripts/mongo-host-user-init.sh:ro" in compose


def test_compose_command_uses_single_authenticated_deployment_file():
    """完整单机 Compose、健康检查和清理使用同一份默认认证配置。"""
    base = root / "deploy" / "docker-compose.yml"
    command = health["_compose"]("camera-log-test", base, root / ".env", "config")
    assert command.count("--file") == 1
    assert command[command.index("--file") + 1] == str(base)


def test_worker_heartbeat_uses_the_worker_configured_database_uri(monkeypatch, tmp_path):
    """完整部署心跳必须查询 Worker 实际使用的数据库，而不是假定内置 mongo1。"""
    calls = []

    def fake_run(command, _deadline):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setitem(health["_worker_heartbeat"].__globals__, "_run", fake_run)
    assert health["_worker_heartbeat"](
        "camera-log-test", tmp_path / "compose.yml", tmp_path / ".env", "worker-1", time.monotonic() + 1,
    )
    command = calls[0]
    assert command[-4:-1] == ["worker", "python", "-c"]
    assert "settings.mongo_uri" in command[-1]
    assert "MONGO_ROOT_PASSWORD=" not in " ".join(command)


def test_health_check_waits_for_services_and_worker_heartbeat(monkeypatch, tmp_path):
    """健康检查必须同时观察副本集、API、前端、worker 运行态及 MongoDB 心跳。"""
    calls = []

    class Result:
        def __init__(self, output="", code=0):
            self.stdout, self.returncode = output, code

    resolved = {
        "services": {
            "api": {"environment": {"DATABASE_NAME": "quoted-db"}},
            "worker": {"environment": {"NODE_ID": "config-worker"}},
            "frontend": {"ports": [{"target": 80, "published": "15173"}]},
        },
    }

    def fake_run(command, _deadline):
        calls.append(command)
        if "config" in command:
            return Result(json.dumps(resolved))
        if command[-3:] and command[-3:-1] == ["ps", "-q"]:
            return Result("container\n")
        if command[:2] == ["docker", "inspect"]:
            return Result("healthy\n" if "Health" in command[3] else "running\n")
        return Result()

    module_globals = health["wait_for_health"].__globals__
    monkeypatch.setitem(module_globals, "_run", fake_run)
    monkeypatch.setitem(module_globals, "time", type("Clock", (), {
        "monotonic": staticmethod(lambda: 0), "sleep": staticmethod(lambda _: None),
    }))
    env_file = tmp_path / ".env"
    env_file.write_text("DATABASE_NAME=misleading\nCOLLECTOR_NODE_ID=misleading\n", encoding="utf-8")
    url = health["wait_for_health"]("camera-log-test", tmp_path / "compose.yml", env_file, 1)
    assert url == "http://127.0.0.1:15173"
    heartbeat = next(command for command in calls if command[-4:-1] == ["worker", "python", "-c"])
    assert "config-worker" in heartbeat[-1] and "settings.mongo_uri" in heartbeat[-1]
    assert any(command[-1] == "frontend" for command in calls)
    compose_calls = [command for command in calls if command[:2] == ["docker", "compose"]]
    assert compose_calls and all("--env-file" in command for command in compose_calls)


def test_health_commands_have_bounded_subprocess_timeout(monkeypatch):
    """单次 Docker 调用必须受总健康检查剩余时间限制。"""
    captured = {}

    def fake_subprocess_run(*_args, **kwargs):
        captured.update(kwargs)
        return subprocess.CompletedProcess([], 0, "", "")

    module_globals = health["_run"].__globals__
    monkeypatch.setitem(module_globals["subprocess"].__dict__, "run", fake_subprocess_run)
    monkeypatch.setitem(module_globals, "time", type("Clock", (), {
        "monotonic": staticmethod(lambda: 100.0),
    }))
    health["_run"](["docker", "info"], 104.0)
    assert captured["timeout"] == 4.0


def test_deploy_script_protects_existing_volumes_before_generating_keys():
    """入口脚本必须在创建环境前检查同项目卷，并使用稳定的默认项目名。"""
    script = (root / "deploy.sh").read_text(encoding="utf-8")
    assert 'project="${COMPOSE_PROJECT_NAME:-camera-log-record-server}"' in script
    assert "docker volume ls -q --filter" in script
    assert "拒绝生成新密钥" in script
    assert "deploy_env.py" in script and "deploy_health.py" in script


def test_deploy_script_uses_sudo_to_revoke_existing_project_nfs_export():
    """空 NFS 地址重跑时，遗留专属 export 也必须走可写系统目录的提权分支。"""
    script = (root / "deploy.sh").read_text(encoding="utf-8")
    assert 'nfs_export_file="/etc/exports.d/camera-logs-coredump.exports"' in script
    assert '[[ -e "$nfs_export_file" && "$EUID" -ne 0 ]]' in script
    assert 'sudo python3 "$root/scripts/configure_nfs_export.py" --env-file "$env_file"' in script


def _deployment_copy(tmp_path):
    """复制部署入口及其最小依赖，以真实 Bash 进程配合假 Docker 验证编排。"""
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True)
    (app / "deploy").mkdir()
    for name in ("deploy.sh",):
        shutil.copy2(root / name, app / name)
    for name in (
        "configure_nfs_export.py", "deploy_env.py", "deploy_docker.sh", "deploy_health.py",
        "deploy_cluster.py", "deploy_component.py",
    ):
        shutil.copy2(root / "scripts" / name, app / "scripts" / name)
    (app / "deploy" / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    return app


def _mock_bin(tmp_path, *, compose=True, volume=False, daemon=True, health=True):
    """提供只记录命令的 Docker 替身，不访问本机 daemon 或创建真实卷。"""
    directory = tmp_path / "bin"
    directory.mkdir()
    (directory / "uname").write_text("#!/bin/sh\necho Linux\n", encoding="utf-8")
    (directory / "grep").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    docker = """#!/bin/sh
if [ -n "$MOCK_LOG" ]; then printf '%s\\n' "$*" >> "$MOCK_LOG"; fi
if [ "$1" = info ]; then exit "$MOCK_DAEMON"; fi
if [ "$1" = volume ]; then [ "$MOCK_VOLUME" = 1 ] && echo existing-volume; exit 0; fi
if [ "$1" = compose ]; then
  shift
  for value in "$@"; do [ "$value" = version ] && exit "$MOCK_COMPOSE"; done
  for value in "$@"; do
    if [ "$value" = config ]; then
      printf '%s\\n' '{"services":{"api":{"environment":{"DATABASE_NAME":"quoted-db"}},"worker":{"environment":{"NODE_ID":"config-worker"}},"frontend":{"ports":[{"target":80,"published":"15173"}]}}}'
      exit 0
    fi
  done
  for value in "$@"; do [ "$value" = up ] && exit 0; done
  for value in "$@"; do [ "$value" = ps ] && { echo fake-container; exit 0; }; done
  for value in "$@"; do [ "$value" = exec ] && exit 0; done
fi
if [ "$1" = inspect ]; then
  case "$3" in *Health*) [ "$MOCK_HEALTH" = 1 ] && echo healthy || echo unhealthy ;; *) echo running ;; esac
  exit 0
fi
exit 0
"""
    if not compose:
        docker = "#!/bin/sh\n[ \"$1\" = compose ] && exit 1\nexit 0\n"
    (directory / "docker").write_text(docker, encoding="utf-8")
    for path in directory.iterdir():
        path.chmod(0o755)
    return directory


def _run_deploy(app, bin_dir, *, cwd=None, **values):
    """以隔离环境运行真实入口，并传入 Docker 状态替身开关。"""
    environment = os.environ | {
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "MOCK_DAEMON": "0" if values.get("daemon", True) else "1",
        "MOCK_VOLUME": "1" if values.get("volume", False) else "0",
        "MOCK_COMPOSE": "0",
        "MOCK_HEALTH": "1" if values.get("health", True) else "0",
        "DEPLOY_HEALTH_TIMEOUT": values.get("timeout", "1"),
        "COMPOSE_PROJECT_NAME": "deploy-mock",
        "MOCK_LOG": str(app / "docker.log"),
    }
    return subprocess.run(["bash", str(app / "deploy.sh")], cwd=cwd or app, env=environment, text=True,
                          capture_output=True, check=False)


def test_deploy_bash_creates_once_and_is_idempotent_with_existing_volume(tmp_path):
    """已有环境与卷允许重跑，真实 Bash 编排不得改写首次生成的配置。"""
    app = _deployment_copy(tmp_path)
    bin_dir = _mock_bin(tmp_path, volume=False)
    first = _run_deploy(app, bin_dir)
    assert first.returncode == 0
    initial = (app / ".env").read_bytes()
    second = _run_deploy(app, bin_dir, volume=True)
    assert second.returncode == 0
    assert (app / ".env").read_bytes() == initial


def test_deploy_bash_uses_root_environment_file_from_any_working_directory(tmp_path):
    """入口从任意目录执行时，Compose 仍必须加载项目根目录的 `.env`。"""
    app = _deployment_copy(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    result = _run_deploy(app, _mock_bin(tmp_path), cwd=elsewhere)
    assert result.returncode == 0
    commands = (app / "docker.log").read_text(encoding="utf-8")
    assert f"--env-file {app / '.env'}" in commands
    assert f"--file {app / 'deploy/docker-compose.yml'} up" in commands


def test_deploy_bash_rejects_missing_environment_when_project_volume_exists(tmp_path):
    """卷存在但密钥文件丢失时，入口必须在 Compose 启动前失败。"""
    app = _deployment_copy(tmp_path)
    result = _run_deploy(app, _mock_bin(tmp_path, volume=True), volume=True)
    assert result.returncode != 0
    assert "拒绝生成新密钥" in result.stderr
    assert not (app / ".env").exists()


def test_deploy_bash_rejects_daemon_permission_failure_before_environment_write(tmp_path):
    """Docker daemon 无权限时不能误判为首次部署并写入新管理员凭据。"""
    app = _deployment_copy(tmp_path)
    result = _run_deploy(app, _mock_bin(tmp_path), daemon=False)
    assert result.returncode != 0
    assert "无法访问 Docker daemon" in result.stderr
    assert not (app / ".env").exists()


def test_deploy_bash_rejects_environment_symlink_without_touching_target(tmp_path):
    """环境文件不得是符号链接，避免管理员密码写入由调用目录外控制的位置。"""
    app = _deployment_copy(tmp_path)
    target = tmp_path / "outside.env"
    target.write_text("preserve", encoding="utf-8")
    (app / ".env").symlink_to(target)
    result = _run_deploy(app, _mock_bin(tmp_path))
    assert result.returncode != 0
    assert "不能是符号链接" in result.stderr
    assert target.read_text(encoding="utf-8") == "preserve"


def test_deploy_bash_reports_compose_missing_and_health_failure(tmp_path):
    """Compose 缺失和健康检查失败必须分别以非零状态结束编排。"""
    missing = _deployment_copy(tmp_path / "missing")
    result = _run_deploy(missing, _mock_bin(tmp_path / "missing", compose=False))
    assert result.returncode != 0
    assert "未找到 Docker Compose" in result.stderr

    unhealthy = _deployment_copy(tmp_path / "unhealthy")
    result = _run_deploy(unhealthy, _mock_bin(tmp_path / "unhealthy", health=False), health=False, timeout="0.01")
    assert result.returncode != 0
    assert "部署健康检查超时" in result.stderr


def test_deploy_script_preserves_non_secret_compose_overrides_after_docker_install():
    """新装 Docker 后以 root 重启入口时必须保留端口等非敏感覆盖。"""
    script = (root / "deploy.sh").read_text(encoding="utf-8")
    assert "--preserve-env=" in script
    assert "FRONTEND_PORT" in script and "DATABASE_NAME" in script


def test_container_smoke_reuses_deploy_environment_for_follow_up_compose_commands():
    """CI 的后续验收、诊断和清理必须显式使用一键部署创建的环境。"""
    workflow = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "prepare_container_env.py" not in workflow
    assert "run: |\n          ./deploy.sh" in workflow
    commands = [line.split("docker compose ", 1)[1] for line in workflow.splitlines() if "docker compose " in line]
    assert commands
    assert all(command.startswith('--env-file .env --project-name "$COMPOSE_PROJECT_NAME" '
                                  '--file deploy/docker-compose.yml ') for command in commands)
