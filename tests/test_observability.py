"""结构化日志回归：递归脱敏、异常堆栈保留和操作者请求关联。"""
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from camera_logs.common import observability


def _read_lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _day_directory(root, instant):
    """测试按处理器固定的上海日零点目录定位当天活动日志。"""
    return root / f"{instant.astimezone(observability.LOG_TIMEZONE):%Y-%m-%dT00-00-00}"


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

    path = next(tmp_path.glob("*T00-00-00/camera-logs.jsonl"))
    content = path.read_text(encoding="utf-8")
    for secret in ("visible-secret", "plain-secret", "another-secret", "ciphertext", "trace-secret", "trace-password"):
        assert secret not in content
    records = _read_lines(path)
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

    directories = list(tmp_path.glob("*T00-00-00"))
    assert len(directories) == 1
    assert (directories[0] / "camera-logs.jsonl").exists()
    assert list(directories[0].glob("camera-logs.*.jsonl"))


def test_service_log_rotation_preserves_existing_shard_symbolic_link(tmp_path, monkeypatch):
    """轮转时把链接视为已占用分片名，不能覆盖或删除它。"""
    current = [datetime(2026, 10, 1, 12, tzinfo=observability.LOG_TIMEZONE)]
    monkeypatch.setattr(observability.DailySizeRotatingFileHandler, "_local_now", staticmethod(lambda: current[0]))
    directory = _day_directory(tmp_path, current[0])
    directory.mkdir()
    outside = tmp_path / "outside.log"
    outside.write_text("outside", encoding="utf-8")
    shard_link = directory / "camera-logs.001.jsonl"
    shard_link.symlink_to(outside)

    handler = observability.DailySizeRotatingFileHandler(tmp_path, max_bytes=1, retention_days=7)
    try:
        handler.handle(logging.makeLogRecord({"name": "test.linked-shard", "levelno": logging.INFO,
                                              "levelname": "INFO", "msg": "first", "args": ()}))
        handler.handle(logging.makeLogRecord({"name": "test.linked-shard", "levelno": logging.INFO,
                                              "levelname": "INFO", "msg": "second", "args": ()}))
    finally:
        handler.close()

    assert shard_link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside"
    assert (directory / "camera-logs.002.jsonl").exists()


def test_service_log_setup_rejects_broken_active_file_symbolic_link(tmp_path, monkeypatch):
    """活动文件即使是断开的符号链接，也不能作为服务日志写入目标。"""
    current = datetime(2026, 10, 1, 12, tzinfo=observability.LOG_TIMEZONE)
    monkeypatch.setattr(observability.DailySizeRotatingFileHandler, "_local_now", staticmethod(lambda: current))
    directory = _day_directory(tmp_path, current)
    directory.mkdir()
    (directory / "camera-logs.jsonl").symlink_to(tmp_path / "missing.log")

    with pytest.raises(OSError, match="不能是符号链接"):
        observability.DailySizeRotatingFileHandler(tmp_path, max_bytes=1, retention_days=7)


def test_midnight_rotation_rejects_new_active_file_symbolic_link_before_moving_old_shard(tmp_path, monkeypatch):
    """午夜目标活动文件被替换为链接时，旧日活动分片必须仍保留且不得写入链接目标。"""
    current = [datetime(2026, 10, 1, 23, 59, tzinfo=observability.LOG_TIMEZONE)]
    monkeypatch.setattr(observability.DailySizeRotatingFileHandler, "_local_now", staticmethod(lambda: current[0]))
    handler = observability.DailySizeRotatingFileHandler(tmp_path, max_bytes=1024, retention_days=7)
    old_active = _day_directory(tmp_path, current[0]) / "camera-logs.jsonl"
    handler.handle(logging.makeLogRecord({"name": "test.midnight-link", "levelno": logging.INFO,
                                          "levelname": "INFO", "msg": "first", "args": ()}))
    outside = tmp_path / "outside.log"
    outside.write_text("outside", encoding="utf-8")
    current[0] = datetime(2026, 10, 2, 0, tzinfo=observability.LOG_TIMEZONE)
    next_directory = _day_directory(tmp_path, current[0])
    next_directory.mkdir()
    (next_directory / "camera-logs.jsonl").symlink_to(outside)

    try:
        with pytest.raises(OSError, match="不能是符号链接"):
            handler.doRollover()
    finally:
        handler.close()

    assert old_active.exists()
    assert outside.read_text(encoding="utf-8") == "outside"


def test_service_logs_rotate_into_shanghai_date_directories_and_clean_expired_logs(tmp_path, monkeypatch):
    """跨上海日期切换目录；过期日期目录只删除自身受管日志并兼容旧根分片。"""
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

    expired_directory = _day_directory(tmp_path, datetime(2026, 10, 1, tzinfo=observability.LOG_TIMEZONE))
    shard = expired_directory / "camera-logs.001.jsonl"
    legacy = tmp_path / "camera-logs.jsonl.1"
    legacy_dated = tmp_path / "camera-logs.2026-10-01.001.jsonl"
    old_root_active = tmp_path / "camera-logs.jsonl"
    unrelated = tmp_path / "device.log"
    legacy.write_text("legacy", encoding="utf-8")
    legacy_dated.write_text("legacy dated", encoding="utf-8")
    root_handle = old_root_active.open("a", encoding="utf-8")
    root_handle.write("old worker remains active\n")
    root_handle.flush()
    unrelated.write_text("keep", encoding="utf-8")
    old_timestamp = datetime(2026, 10, 1, tzinfo=observability.LOG_TIMEZONE).timestamp()
    import os
    os.utime(shard, (old_timestamp, old_timestamp))
    os.utime(legacy, (old_timestamp, old_timestamp))
    os.utime(legacy_dated, (old_timestamp, old_timestamp))
    os.utime(old_root_active, (old_timestamp, old_timestamp))
    handler.cleanup_expired(reference=datetime(2026, 10, 9, tzinfo=observability.LOG_TIMEZONE))
    root_handle.write("still held by old worker\n")
    root_handle.close()

    assert not shard.exists() and not expired_directory.exists()
    assert not legacy.exists()
    assert not legacy_dated.exists()
    assert old_root_active.read_text(encoding="utf-8") == "old worker remains active\nstill held by old worker\n"
    assert (_day_directory(tmp_path, current[0]) / "camera-logs.jsonl").exists()
    assert unrelated.exists()


def test_service_log_retention_hot_update_cleans_expired_directories_immediately(tmp_path):
    """管理员热更新在线程中立即清理过期日期目录，无需等待下一条运行日志。"""
    listener = observability.setup_logging(tmp_path, retention_days=7)
    expired_directory = tmp_path / "2026-01-01T00-00-00"
    expired_directory.mkdir()
    expired = expired_directory / "camera-logs.001.jsonl"
    expired.write_text("old", encoding="utf-8")
    old_timestamp = datetime(2026, 1, 1, tzinfo=observability.LOG_TIMEZONE).timestamp()
    import os
    os.utime(expired, (old_timestamp, old_timestamp))
    try:
        observability.set_service_log_retention_days(1)
        assert not expired.exists()
        assert not expired_directory.exists()
    finally:
        listener.stop()


def test_expired_service_log_directory_keeps_unknown_files_and_links(tmp_path):
    """受管分片可清理，但未知文件和链接绝不能被递归删除或使目录误删。"""
    listener = observability.setup_logging(tmp_path, retention_days=1)
    expired_directory = tmp_path / "2026-01-01T00-00-00"
    expired_directory.mkdir()
    managed = expired_directory / "camera-logs.001.jsonl"
    managed.write_text("old", encoding="utf-8")
    outside = tmp_path / "outside.log"
    outside.write_text("outside", encoding="utf-8")
    link = expired_directory / "camera-logs.002.jsonl"
    link.symlink_to(outside)
    unknown = expired_directory / "keep.txt"
    unknown.write_text("keep", encoding="utf-8")
    try:
        handler = next(handler for handler in listener.handlers if isinstance(handler, observability.DailySizeRotatingFileHandler))
        handler.cleanup_expired(reference=datetime(2026, 10, 10, tzinfo=observability.LOG_TIMEZONE))
        assert not managed.exists()
        assert link.is_symlink() and outside.read_text(encoding="utf-8") == "outside"
        assert unknown.read_text(encoding="utf-8") == "keep"
        assert expired_directory.is_dir()
    finally:
        listener.stop()


def test_expired_service_log_directory_reports_permission_error_without_recursive_logging(tmp_path, monkeypatch, capsys):
    """日期目录中的受管分片删除失败要保留文件并输出诊断，不经运行日志 handler 再入。"""
    handler = observability.DailySizeRotatingFileHandler(tmp_path, max_bytes=1024, retention_days=1)
    expired_directory = tmp_path / "2026-01-01T00-00-00"
    expired_directory.mkdir()
    managed = expired_directory / "camera-logs.001.jsonl"
    managed.write_text("old", encoding="utf-8")
    original_unlink = Path.unlink

    def reject_managed_unlink(path, *args, **kwargs):
        if path == managed:
            raise PermissionError("denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", reject_managed_unlink)
    try:
        handler.cleanup_expired(reference=datetime(2026, 10, 10, tzinfo=observability.LOG_TIMEZONE))
    finally:
        handler.close()

    assert managed.exists()
    assert "WARNING 服务运行日志日期目录分片失败" in capsys.readouterr().err


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


def test_successful_http_client_polling_does_not_flood_service_logs(tmp_path):
    """高频内部读取不逐次入队；告警与平台访问审计仍保留。"""
    listener = observability.setup_logging(tmp_path)
    try:
        logging.getLogger("httpx").info("HTTP Request: GET http://worker/internal/tail/task HTTP/1.1 200 OK")
        logging.getLogger("httpx").warning("upstream warning")
        logging.getLogger("asyncssh").info("third-party connection opened")
        logging.getLogger("asyncssh").warning("third-party connection warning")
        logging.getLogger("telnetlib3.client").info("third-party telnet opened")
        logging.getLogger("telnetlib3.client").warning("third-party telnet warning")
        logging.getLogger("camera_logs.collection.runtime").info("平台采集运行状态")
        logging.getLogger("camera_logs.access").info("request completed", extra={"context": {"requestId": "kept"}})
    finally:
        listener.stop()
    rows = _read_lines(next(tmp_path.glob("*T00-00-00/camera-logs.jsonl")))
    assert [row["message"] for row in rows] == [
        "upstream warning", "third-party connection warning", "third-party telnet warning",
        "平台采集运行状态", "request completed",
    ]
    assert rows[-1]["context"]["requestId"] == "kept"
