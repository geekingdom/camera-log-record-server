"""同一串口服务器的不同端口必须使用独立幂等键注册任务。"""

import json
import runpy
import sys
from pathlib import Path

import httpx


def test_registration_supports_two_ports_on_one_serial_server(tmp_path, monkeypatch):
    devices = [{"name": f"serial-{port}", "ip": "192.0.2.1", "port": port, "protocol": "TELNET_SERIAL"}
        for port in (10002, 10003)]
    path = tmp_path / "devices.json"
    path.write_text(json.dumps(devices))
    created = {}

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
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
