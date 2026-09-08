"""同一串口服务器的不同端口必须使用独立幂等键注册任务。"""

import json
import runpy
import sys
from pathlib import Path

import httpx
import pytest


def test_registration_supports_two_ports_on_one_serial_server(tmp_path, monkeypatch):
    """每个清单任务直接发送创建请求，端口相同或不同均由幂等键区分。"""
    devices = [{"name": f"serial-{port}", "ip": "192.0.2.1", "port": port, "protocol": "TELNET_SERIAL", "resourceId": "serial-resource"}
        for port in (10002, 10003)]
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(devices))
    created = {}

    def respond(request):
        assert request.method == "POST"
        key = request.headers["Idempotency-Key"]
        if key in created:
            return httpx.Response(409, json={"error": "idempotency key conflict"})
        body = json.loads(request.content)
        created[key] = body
        return httpx.Response(201, json={"id": str(body["port"]), "name": body["name"], "status": "STOPPED"})

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "synthetic-test-token")
    monkeypatch.setattr(sys, "argv", ["register_local_devices.py", "--devices", str(path)])
    script = Path(__file__).resolve().parents[1] / "scripts/register_local_devices.py"
    runpy.run_path(str(script), run_name="__main__")
    assert len(created) == 2
    assert {body["port"] for body in created.values()} == {10002, 10003}
    assert {body["resourceId"] for body in created.values()} == {"serial-resource"}


def test_registration_reports_missing_resource_without_creating_task(tmp_path, monkeypatch):
    """新设备未提供资源 ID 时应给出清晰提示，且不能发送任务创建请求。"""
    path = tmp_path / "devices.json"
    path.write_text(json.dumps([{"name": "missing", "ip": "192.0.2.2", "port": 23, "protocol": "TELNET_DEVICE"}]))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(500)

    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setenv("BOOTSTRAP_TOKEN", "synthetic-test-token")
    monkeypatch.setattr(sys, "argv", ["register_local_devices.py", "--devices", str(path)])
    script = Path(__file__).resolve().parents[1] / "scripts/register_local_devices.py"

    with pytest.raises(ValueError, match="缺少 resourceId"):
        runpy.run_path(str(script), run_name="__main__")
    assert requests == []
