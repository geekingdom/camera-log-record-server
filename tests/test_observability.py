"""结构化日志回归：递归脱敏、异常堆栈保留和操作者请求关联。"""
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime

import pytest
from camera_logs.common import observability


def _read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("host_zone", ["UTC", "America/Los_Angeles", "Asia/Shanghai"])
def test_json_timestamp_uses_shanghai_regardless_of_host_timezone(host_zone):
    """同一事件在UTC容器或任意宿主时区均输出跨日北京时间，且标明偏移。"""
    # 使用独立进程改变TZ，不干扰测试主进程的后台日志线程。
    source = """
import logging
from datetime import UTC, datetime
from camera_logs.common.observability import JsonLineFormatter
record = logging.LogRecord('test.timezone', logging.INFO, '', 1, 'timezone', (), None)
record.created = datetime(2026, 9, 15, 20, 30, 12, tzinfo=UTC).timestamp()
print(JsonLineFormatter().format(record))
"""
    result = subprocess.run([sys.executable, "-c", source], env={**os.environ, "TZ": host_zone},
                            capture_output=True, text=True, check=True, timeout=10)
    payload = json.loads(result.stdout)
    assert payload["timestamp"] == "2026-09-16T04:30:12+08:00"
    assert datetime.fromisoformat(payload["timestamp"]).timestamp() == datetime(2026, 9, 15, 20, 30, 12, tzinfo=UTC).timestamp()


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
