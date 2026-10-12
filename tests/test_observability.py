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
    """单日内达到大小上限会生成可按日期审计的连续分片。"""
    monkeypatch.setattr(observability, "MAX_LOG_BYTES", 180)
    listener = observability.setup_logging(tmp_path)
    try:
        logger = logging.getLogger("test.rotation")
        for index in range(20):
            logger.info("event %s %s", index, "x" * 80)
    finally:
        listener.stop()

    assert (tmp_path / "camera-logs.jsonl").exists()
    assert list(tmp_path.glob("camera-logs.*.jsonl"))


def test_service_logs_rotate_at_shanghai_day_boundary_and_clean_only_expired_shards(tmp_path, monkeypatch):
    """跨上海日期立即切片；过期新旧分片可删，但活动文件和同目录其它文件绝不误删。"""
    current = [datetime(2026, 10, 1, 23, 59, tzinfo=observability.LOG_TIMEZONE)]
    monkeypatch.setattr(observability.DailySizeRotatingFileHandler, "_local_now", staticmethod(lambda: current[0]))
    listener = observability.setup_logging(tmp_path, retention_days=7)
    try:
        handler = next(handler for handler in listener.handlers if isinstance(handler, observability.DailySizeRotatingFileHandler))
        handler.handle(logging.makeLogRecord({"name": "test.day-rotation", "levelno": logging.INFO,
                                              "levelname": "INFO", "msg": "first day", "args": ()}))
        current[0] = datetime(2026, 10, 2, 0, 0, tzinfo=observability.LOG_TIMEZONE)
        handler.handle(logging.makeLogRecord({"name": "test.day-rotation", "levelno": logging.INFO,
                                              "levelname": "INFO", "msg": "second day", "args": ()}))
    finally:
        listener.stop()

    shard = tmp_path / "camera-logs.2026-10-01.001.jsonl"
    legacy = tmp_path / "camera-logs.jsonl.1"
    unrelated = tmp_path / "device.log"
    legacy.write_text("legacy", encoding="utf-8")
    unrelated.write_text("keep", encoding="utf-8")
    old_timestamp = datetime(2026, 10, 1, tzinfo=observability.LOG_TIMEZONE).timestamp()
    import os
    os.utime(shard, (old_timestamp, old_timestamp))
    os.utime(legacy, (old_timestamp, old_timestamp))
    handler.cleanup_expired(reference=datetime(2026, 10, 10, tzinfo=observability.LOG_TIMEZONE))

    assert not shard.exists()
    assert not legacy.exists()
    assert (tmp_path / "camera-logs.jsonl").exists()
    assert unrelated.exists()


def test_service_log_retention_hot_update_cleans_expired_files(tmp_path):
    """管理员热更新由下一条后台队列日志清理过期文件，不阻塞 API 事件循环。"""
    listener = observability.setup_logging(tmp_path, retention_days=7)
    expired = tmp_path / "camera-logs.2026-01-01.001.jsonl"
    expired.write_text("old", encoding="utf-8")
    old_timestamp = datetime(2026, 1, 1, tzinfo=observability.LOG_TIMEZONE).timestamp()
    import os
    os.utime(expired, (old_timestamp, old_timestamp))
    try:
        observability.set_service_log_retention_days(1)
        handler = next(handler for handler in listener.handlers if isinstance(handler, observability.DailySizeRotatingFileHandler))
        handler.handle(logging.makeLogRecord({"name": "test.hot-update", "levelno": logging.INFO,
                                              "levelname": "INFO", "msg": "apply retention", "args": ()}))
        assert not expired.exists()
    finally:
        listener.stop()


def test_json_logging_bounds_large_exception_trace_after_redaction():
    """异常消息再大也不能独占运行日志分片，截断前仍不得泄露凭据。"""
    formatter = observability.JsonLineFormatter()
    secret = "token=visible-secret"
    try:
        raise RuntimeError((secret + " x") * 30_000)
    except RuntimeError:
        record = logging.getLogger("test.exception-bound").makeRecord(
            "test.exception-bound", logging.ERROR, __file__, 1, "failed", (), sys.exc_info(),
        )
    payload = json.loads(formatter.format(record))
    assert "visible-secret" not in payload["exception"]
    assert "[异常追踪已截断，保留首尾]" in payload["exception"]
    assert len(payload["exception"].encode("utf-8")) <= observability.MAX_EXCEPTION_TEXT_BYTES


def test_exception_utf8_boundary_finishes_without_blocking_listener():
    """首部只余两个字节时中文异常必须转入尾部，不能卡死日志监听器。"""
    code = """
from camera_logs.common import observability as o
class Trace:
    def __init__(self, *args): pass
    def format(self, **kwargs):
        return iter(['a' * 4096] * 3 + ['b' * 4094, '中文异常结尾'])
o.traceback.TracebackException = Trace
result = o._bounded_exception_text((None, None, None))
if '中文异常结尾' not in result or len(result.encode()) > o.MAX_EXCEPTION_TEXT_BYTES:
    raise RuntimeError('exception truncation lost tail or exceeded budget')
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=5)
