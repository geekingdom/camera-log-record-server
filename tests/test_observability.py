"""结构化日志回归：递归脱敏、异常堆栈保留和操作者请求关联。"""
import json
import logging

from camera_logs.common import observability


def _read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_json_logging_redacts_nested_values_and_exception_trace(tmp_path, capsys):
    listener = observability.setup_logging(tmp_path)
    try:
        logger = logging.getLogger("test.observability")
        logger.info("operation token=visible-secret", extra={"context": {
            "password": "plain-secret",
            "nested": [{"authorization": "Bearer another-secret"}],
            "passwordEncrypted": "ciphertext",
        }})
        try:
            raise RuntimeError("authorization: Bearer trace-secret password=trace-password")
        except RuntimeError:
            logger.exception("request failed")
    finally:
        listener.stop()

    content = (tmp_path / "camera-logs.jsonl").read_text(encoding="utf-8")
    for secret in ("visible-secret", "plain-secret", "another-secret", "ciphertext", "trace-secret", "trace-password"):
        assert secret not in content
    records = _read_lines(tmp_path / "camera-logs.jsonl")
    assert records[0]["context"]["password"] == "[REDACTED]"
    assert records[0]["context"]["nested"][0]["authorization"] == "[REDACTED]"
    assert "[REDACTED]" in records[1]["exception"]
    assert "[REDACTED]" in capsys.readouterr().out


def test_rotating_jsonl_keeps_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(observability, "MAX_LOG_BYTES", 180)
    listener = observability.setup_logging(tmp_path)
    try:
        logger = logging.getLogger("test.rotation")
        for index in range(20):
            logger.info("event %s %s", index, "x" * 80)
    finally:
        listener.stop()

    assert (tmp_path / "camera-logs.jsonl").exists()
    assert list(tmp_path.glob("camera-logs.jsonl.*"))
