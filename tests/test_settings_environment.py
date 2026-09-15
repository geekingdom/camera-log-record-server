"""验证部署字符串配置与严格整数范围兼容，防止容器及systemd启动失败。"""

import pytest
from camera_logs.common.config import Settings
from pydantic import ValidationError

INTEGER_SETTINGS = {
    "node_capacity": 120,
    "cluster_capacity": 600,
    "retention_days": 7,
    "authentication_record_retention_days": 90,
    "audit_record_retention_days": 0,
    "runtime_event_retention_days": 0,
}


def test_psh_http_environment_uses_public_default_endpoints_and_configurable_timeouts(monkeypatch):
    """生产 Worker 可只外置认证标识，并用环境调整公司网络等待预算。"""
    monkeypatch.setenv("PSH_MODE", "http")
    monkeypatch.setenv("PSH_CLIENT_ID", "test-client")
    monkeypatch.setenv("PSH_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("PSH_API_KEY", "test-key")
    monkeypatch.setenv("PSH_USER_NAME", "test-user")
    monkeypatch.setenv("PSH_REQUEST_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("PSH_TOTAL_TIMEOUT_SECONDS", "70")
    settings = Settings(_env_file=None)
    assert settings.psh_token_url == "https://hicode-auth-hz.hikvision.com/oauth/token"
    assert settings.psh_api_url == "https://itapi.hikvision.com/api/"
    assert settings.psh_request_timeout_seconds == 20
    assert settings.psh_total_timeout_seconds == 70


def test_psh_total_timeout_cannot_be_shorter_than_one_request():
    """部署文件中的超时关系在进程启动前拒绝，而不是调试时才出现矛盾。"""
    with pytest.raises(ValidationError, match="总超时"):
        Settings(_env_file=None, psh_request_timeout_seconds=10, psh_total_timeout_seconds=9)


def test_integer_settings_from_process_environment(monkeypatch):
    """Docker和systemd均以字符串传递环境变量，合法配置必须能够启动。"""
    for field, value in INTEGER_SETTINGS.items():
        monkeypatch.setenv(field.upper(), str(value))
    settings = Settings(_env_file=None)
    for field, value in INTEGER_SETTINGS.items():
        assert getattr(settings, field) == value


def test_integer_settings_from_dotenv(tmp_path, monkeypatch):
    """直接读取部署.env也使用相同规则，并避开当前机器同名环境变量。"""
    path = tmp_path / "deployment.env"
    path.write_text("\n".join(f"{field.upper()}={value}" for field, value in INTEGER_SETTINGS.items()))
    for field in INTEGER_SETTINGS:
        monkeypatch.delenv(field.upper(), raising=False)
    settings = Settings(_env_file=path)
    for field, value in INTEGER_SETTINGS.items():
        assert getattr(settings, field) == value


@pytest.mark.parametrize("field", INTEGER_SETTINGS)
@pytest.mark.parametrize("value", [True, 1.0, "1.0", "1e2", "true", "", "-1", "10001"])
def test_integer_settings_reject_invalid_values(field, value):
    """环境文本兼容不得把布尔、小数或超范围值静默转为合法配置。"""
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
