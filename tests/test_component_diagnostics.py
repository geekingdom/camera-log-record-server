"""独立部署失败诊断的脱敏与有限类别提取测试。"""

import importlib.util
import sys
from pathlib import Path
from urllib.parse import quote, quote_plus

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "scripts"))
spec = importlib.util.spec_from_file_location("component_diagnostics", root / "scripts" / "component_diagnostics.py")
diagnostics = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(diagnostics)


def test_diagnostic_redacts_exact_url_encoded_and_repeated_secrets_before_truncation():
    """已知临时环境值的原文、URI 编码和重复 Bearer 前缀均不能泄露。"""
    password = "Pa ss/word+42"
    token = "Bearer temporary-token"
    output = diagnostics.diagnose_process_failure(
        "数据库启动", 1,
        "", f"Error response from daemon: pull access denied; {password}; {quote(password, safe='')}; "
        f"{quote_plus(password)}; Bearer {token}", secrets=[password, token],
    )

    assert "镜像" in output
    assert password not in output
    assert quote(password, safe="") not in output
    assert quote_plus(password) not in output
    assert "temporary-token" not in output
    assert "原始输出已隐藏" in output


def test_diagnostic_redacts_uri_credentials_and_actual_or_escaped_newlines():
    """Mongo URI 的用户名密码及跨行密钥在分行和转义形式中均先被替换。"""
    username, password, key = "camera_admin", "p@ss:word", "line-one\nline-two"
    uri = f"mongodb://{username}:{quote(password, safe='')}@mongo:27017/camera_logs"
    output = diagnostics.diagnose_process_failure(
        "后端启动", 1, "", f"connection refused for {uri}\nlookup db: no such host\n{key!r}",
        secrets=[username, password, key, uri],
    )

    assert "DNS" in output and "连接" in output
    for value in (username, password, quote(password, safe=""), key, "line-one\\nline-two", uri):
        assert value not in output


def test_diagnostic_strips_ansi_and_limits_sanitized_evidence_after_redaction():
    """ANSI 控制符不会绕过匹配；长证据只在完成脱敏后截断。"""
    secret = "very-secret-value"
    evidence = "\x1b[31mBind for 0.0.0.0:18080 failed: port is already allocated " + secret + "\x1b[0m"
    output = diagnostics.diagnose_process_failure("后端启动", 1, "", evidence + "x" * 500, secrets=[secret], max_length=180)

    assert "端口" in output
    assert "\x1b" not in output
    assert secret not in output
    assert len(output) <= 180


def test_diagnostic_classifies_registry_limit_dns_connection_port_and_compose_errors():
    """常见独立部署失败输出能给出安全类别和有限定位证据。"""
    cases = [
        ("toomanyrequests: You have reached your pull rate limit", "Registry 限流"),
        ("lookup registry.example: no such host", "DNS"),
        ("dial tcp 10.0.0.2:443: connect: connection refused", "连接"),
        ("Bind for 0.0.0.0:18080 failed: port is already allocated", "端口"),
        ("validating compose.yml: services.api.additional property typo is not allowed", "Compose 配置"),
    ]

    for source, expected in cases:
        output = diagnostics.diagnose_process_failure("组件部署", 1, "", source)
        assert expected in output


def test_unknown_output_is_not_echoed_and_is_marked_unclassified():
    """未知错误不能因方便排查而直接回显可能包含配置的数据。"""
    output = diagnostics.diagnose_process_failure(
        "组件部署", 1, "", "unrecognized failure payload may contain configuration", secrets=["configuration"],
    )

    assert "未分类" in output
    assert "unrecognized failure" not in output
    assert "configuration" not in output


def test_classified_lines_also_hide_unlisted_credentials():
    """已知故障行也可能包含外部registry凭据，不得仅依赖本轮生成的密码列表。"""
    output = diagnostics.diagnose_process_failure(
        "镜像部署", 1, "", "pull access denied https://registry-user:registry-pass@registry.example/image; "
        'PASSWORD="external password"; authorization=Bearer external-token; API_KEY=external-key',
    )
    assert "镜像" in output
    for secret in ("registry-user", "registry-pass", "external password", "external-token", "external-key"):
        assert secret not in output


def test_unlisted_credentials_with_spaces_and_basic_authorization_are_hidden():
    """未知认证字段整段隐藏，不能遗漏空格后的口令或认证方案后的编码值。"""
    for value in ("PASSWORD=external password", "authorization: Basic dXNlcjpwYXNz"):
        output = diagnostics.diagnose_process_failure("镜像部署", 1, "", f"pull access denied; {value}")
        assert "镜像" in output
        assert "external" not in output
        assert " password" not in output
        assert "dXNlcjpwYXNz" not in output


def test_classified_output_never_echoes_freeform_external_credentials():
    """第三方错误没有固定凭据格式，只允许输出分类与固定提示。"""
    for value in ("password is external-secret", "X-Registry-Auth: dXNlcjpwYXNz", "opaque external-secret"):
        output = diagnostics.diagnose_process_failure("镜像部署", 1, "", f"pull access denied; {value}")
        assert "镜像获取" in output
        assert "external-secret" not in output
        assert "dXNlcjpwYXNz" not in output
