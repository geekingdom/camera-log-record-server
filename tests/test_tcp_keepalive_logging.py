"""TCP保活配置降级必须可诊断，但不能阻止协议连接或记录原始异常中的凭据。"""

import errno
import logging
import socket
from types import SimpleNamespace

import pytest
from camera_logs.collection.connections import _tcp_keepalive


@pytest.mark.parametrize("failed_option", [socket.SO_KEEPALIVE, 901])
def test_keepalive_failure_logs_failed_option_without_raising_or_secret(monkeypatch, caplog, failed_option):
    monkeypatch.setattr(socket, "TCP_KEEPIDLE", 901, raising=False)
    monkeypatch.setattr(socket, "TCP_KEEPINTVL", 902, raising=False)
    monkeypatch.setattr(socket, "TCP_KEEPCNT", 903, raising=False)

    def set_option(level, option, value):
        if option == failed_option:
            raise OSError(errno.EPERM, "synthetic-private-credential")

    owner = SimpleNamespace(get_extra_info=lambda key: SimpleNamespace(setsockopt=set_option))
    with caplog.at_level(logging.WARNING, logger="camera_logs.collection.connections"):
        _tcp_keepalive(owner)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert "TCP 保活配置失败" in record.getMessage()
    assert record.context["socketOption"] == ("SO_KEEPALIVE" if failed_option == socket.SO_KEEPALIVE else "TCP_KEEPIDLE")
    assert record.context["errno"] == errno.EPERM
    assert record.context["errorType"] == "PermissionError"
    assert record.exc_info is None
    assert "synthetic-private-credential" not in str(record.__dict__)


def test_successful_keepalive_configuration_does_not_log_warning(caplog):
    calls = []
    owner = SimpleNamespace(get_extra_info=lambda key: SimpleNamespace(setsockopt=lambda *args: calls.append(args)))
    with caplog.at_level(logging.WARNING, logger="camera_logs.collection.connections"):
        _tcp_keepalive(owner)
    assert (socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1) in calls
    assert not caplog.records
