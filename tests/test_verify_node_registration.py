"""验证隔离节点登记脚本从 Compose 最终配置读取实际 worker 身份。"""

import json
import runpy
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

root = Path(__file__).resolve().parents[1]
module = runpy.run_path(str(root / "scripts/verify_node_registration.py"))


def _args(**overrides):
    """构造不访问 Docker 的解析参数。"""
    values = {
        "node_id": None,
        "node_url": None,
        "project": "camera-logs",
        "compose_file": Path("deploy/docker-compose.yml"),
        "env_file": Path(".env"),
        "timeout": 30,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _config(node_id="compose-worker-1", node_url="http://worker:18081"):
    """构造 Compose JSON 中实际传递给 worker 的非敏感配置。"""
    return json.dumps(
        {
            "services": {
                "worker": {
                    "environment": {
                        "NODE_ID": node_id,
                        "NODE_URL": node_url,
                    }
                }
            }
        }
    )


def test_default_node_identity_uses_resolved_compose_worker(monkeypatch):
    """未提供节点参数时不能再依赖过期硬编码地址。"""
    calls = []

    def fake_run(command, _deadline):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, _config(), "")

    monkeypatch.setitem(module["_resolved"].__globals__, "_run", fake_run)
    assert module["_node_identity"](_args(), time.monotonic() + 1) == (
        "compose-worker-1",
        "http://worker:18081",
    )
    assert calls[0][-3:] == ["config", "--format", "json"]
    assert "--env-file" in calls[0] and "--project-name" in calls[0]


def test_custom_compose_worker_identity_is_used(monkeypatch, tmp_path):
    """项目、Compose 文件和环境文件显式传入时必须用于最终配置解析。"""
    captured = []

    def fake_run(command, _deadline):
        captured.extend(command)
        return subprocess.CompletedProcess(command, 0, _config("edge-a", "http://edge-a:19081"), "")

    monkeypatch.setitem(module["_resolved"].__globals__, "_run", fake_run)
    args = _args(
        project="custom-project", compose_file=tmp_path / "compose.yml", env_file=tmp_path / "node.env"
    )
    assert module["_node_identity"](args, time.monotonic() + 1) == ("edge-a", "http://edge-a:19081")
    assert ["--project-name", "custom-project"] == captured[
        captured.index("--project-name") : captured.index("--project-name") + 2
    ]
    assert str(args.compose_file) in captured and str(args.env_file) in captured


def test_explicit_node_identity_skips_compose_resolution(monkeypatch):
    """调用者已有明确节点身份时不依赖 Docker 或配置文件。"""
    monkeypatch.setitem(
        module["_node_identity"].__globals__,
        "_resolved",
        lambda *_args: pytest.fail("显式节点参数不应调用 Compose"),
    )
    args = _args(node_id="manual-node", node_url="http://manual-node:18081")
    assert module["_node_identity"](args, time.monotonic() + 1) == ("manual-node", "http://manual-node:18081")


@pytest.mark.parametrize(
    "output", ["not-json", json.dumps({"services": {}}), json.dumps({"services": {"worker": []}})]
)
def test_compose_parse_failure_is_safe_and_specific(monkeypatch, output):
    """坏输出不回显 Compose 内容，且缺服务不会退回错误默认地址。"""
    monkeypatch.setitem(
        module["_resolved"].__globals__,
        "_run",
        lambda command, _deadline: subprocess.CompletedProcess(command, 0, output, "sensitive"),
    )
    with pytest.raises(RuntimeError, match="Docker Compose 最终配置") as error:
        module["_node_identity"](_args(), time.monotonic() + 1)
    assert output not in str(error.value)


def test_compose_runner_handles_missing_docker_without_output(monkeypatch):
    """Docker 不可执行时返回有界失败，后续仅给出通用配置读取错误。"""

    def unavailable(*_args, **_kwargs):
        raise OSError("private command detail")

    monkeypatch.setitem(module["_run"].__globals__["subprocess"].__dict__, "run", unavailable)
    result = module["_run"](["docker", "compose"], time.monotonic() + 1)
    assert result.returncode == 127
    assert result.stdout == "" and result.stderr == "command unavailable"
