"""验证PSH排障文本保留有效原因并清除URL与编码凭据。"""

from camera_logs.collection.psh_diagnostics import (
    body_preview,
    diagnostic,
    response_details,
    safe_text,
    started_at,
)


def test_exception_keeps_tls_cause_but_removes_url_credentials():
    """保留证书失败原因，异常里嵌入的URL查询串也不能绕过脱敏。"""
    error = ConnectionError("TLS certificate verify failed at https://u:p@example.test/token?private=secret")
    details = diagnostic("TOKEN_REQUEST", "HTTP_ERROR", started=started_at(), error=error)
    assert "TLS certificate verify failed" in details["exceptionMessage"]
    assert "https://example.test/token" in details["exceptionMessage"]
    assert "private" not in details["exceptionMessage"]
    assert "u:p" not in details["exceptionMessage"]


def test_short_and_encoded_credentials_are_removed_before_preview_cut():
    """秘密跨512字节预览边界、URL编码回显或标准认证字段时均不保留。"""
    value = "x " * 250 + "short-secret"
    preview = body_preview(value.encode(), "text/plain", ("short-secret",))
    assert "short" not in preview["responsePreview"]
    text = safe_text('denied a%2Bb%21 client_secret="other-secret" access_token=another', ("a+b!",))
    assert "a%2Bb" not in text and "other-secret" not in text and "another" not in text


def test_error_response_cannot_echo_its_own_token_or_password():
    details = response_details({"code": "403003", "access_token": "short-token",
        "data": {"data": "short-pass"}, "message": "rejected short-token and short-pass"})
    assert details["serviceCode"] == "403003"
    assert "short-token" not in str(details) and "short-pass" not in str(details)
