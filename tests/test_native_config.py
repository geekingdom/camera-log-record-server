"""原生部署配置和渲染的安全边界回归。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from native_config import create_config, defaults, read_config, validate
from native_units import environment, render


def test_create_is_exclusive_and_read_never_evaluates(tmp_path):
    path = tmp_path / "native.env"
    create_config(path)
    before = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        create_config(path)
    assert path.read_bytes() == before
    path.write_text('PASSWORD=$(touch /should-not-exist)\n')
    assert read_config(path)["PASSWORD"] == '$(touch /should-not-exist)'
    path.write_text('PASSWORD=secret\nPASSWORD=other\n')
    with pytest.raises(ValueError, match="键重复"):
        read_config(path)


@pytest.mark.parametrize("key,value", [("INSTALL_ROOT", "/"), ("LOG_ROOT", "/tmp/../etc"),
    ("SERVICE_USER", "root"), ("FORWARDED_ALLOW_IPS", "*"), ("MONGO_PORT", "0"),
    ("BACKEND_UPSTREAM", "http://localhost;bad"), ("MONGO_BIND_IP", "0.0.0.0")])
def test_invalid_configuration_rejected_before_install(key, value):
    values = defaults() | {key: value}
    with pytest.raises(ValueError):
        validate(values, "all")


def test_native_units_keep_proxy_boundary_and_literal_secrets():
    values = defaults()
    validate(values, "all")
    result = render(values, Path(__file__).resolve().parents[1])
    for component in result.values():
        unit = next(value for name, value in component.items() if name.endswith(".service"))
        assert "Restart=on-failure" in unit and "WantedBy=multi-user.target" in unit
        assert "User=camera-logs" in unit
    assert "proxy_set_header X-Forwarded-For $remote_addr" in result["frontend"]["nginx.conf"]
    assert "MONGO_ADMIN_PASSWORD=" not in result["backend"]["api.env"]
    assert json.loads(result["database"]["mongod.conf"])["security"]["authorization"] == "enabled"
    assert environment({"PASSWORD": 'x"y\\z$abc'}) == 'PASSWORD="x\\"y\\\\z$abc"\n'


def test_independent_worker_requires_platform_credentials(tmp_path):
    path = tmp_path / "worker.env"
    create_config(path, "worker")
    with pytest.raises(ValueError, match="请填写"):
        validate(read_config(path), "worker")
