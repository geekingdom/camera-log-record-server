"""验证 API 时间只在展示边界转换为上海时区。"""

from datetime import UTC, datetime

from camera_logs.common.config import Settings
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi import WebSocket
from mongomock_motor import AsyncMongoMockClient
from pydantic import BaseModel
from starlette.testclient import TestClient


class _TimestampModel(BaseModel):
    """用于覆盖 FastAPI 的 Pydantic 响应编码路径。"""

    createdAt: datetime


def _client(tmp_path):
    """建立不启动后台任务的隔离 API 应用。"""
    settings = Settings(
        _env_file=None,
        bootstrap_token="presentation-time",
        encryption_key=Fernet.generate_key().decode(),
        log_root=tmp_path,
        start_background=False,
    )
    return TestClient(create_app(settings, AsyncMongoMockClient().camera_logs))


def test_http_pydantic_and_websocket_timestamps_are_shanghai_time(tmp_path):
    """HTTP 各编码路径和实时帧都把绝对时刻展示为 +08:00。"""
    stamp = datetime(2026, 10, 16, 4, 30, tzinfo=UTC)
    with _client(tmp_path) as client:
        @client.app.get("/api/v1/presentation-time-dict")
        async def dictionary():
            return {
                "createdAt": stamp,
                "updatedAt": stamp.replace(tzinfo=None),
                "nested": {"receivedAt": stamp.isoformat()},
                "hour": stamp.isoformat(),
                "hourId": stamp.isoformat(),
                "text": stamp.isoformat(),
                "command": stamp.isoformat(),
                "password": stamp.isoformat(),
                "id": stamp.isoformat(),
            }

        @client.app.get("/api/v1/presentation-time-model", response_model=_TimestampModel)
        async def model():
            return _TimestampModel(createdAt=stamp)

        @client.app.websocket("/api/v1/presentation-time-stream")
        async def stream(socket: WebSocket):
            await socket.accept()
            await socket.send_json({"receivedAt": stamp.isoformat()})

        assert client.get("/api/v1/presentation-time-dict").json() == {
            "createdAt": "2026-10-16T12:30:00+08:00",
            "updatedAt": "2026-10-16T12:30:00+08:00",
            "nested": {"receivedAt": "2026-10-16T12:30:00+08:00"},
            "hour": "2026-10-16T12:30:00+08:00",
            "hourId": "2026-10-16T04:30:00+00:00",
            "text": "2026-10-16T04:30:00+00:00",
            "command": "2026-10-16T04:30:00+00:00",
            "password": "2026-10-16T04:30:00+00:00",
            "id": "2026-10-16T04:30:00+00:00",
        }
        assert client.get("/api/v1/presentation-time-model").json() == {
            "createdAt": "2026-10-16T12:30:00+08:00",
        }
        with client.websocket_connect("/api/v1/presentation-time-stream") as socket:
            assert socket.receive_json() == {"receivedAt": "2026-10-16T12:30:00+08:00"}


def test_shanghai_hour_queries_keep_legacy_utc_catalog_visible(tmp_path):
    """上海日与范围提交均能匹配旧版 UTC ISO 小时目录键。"""
    with _client(tmp_path) as client:
        client.headers["Authorization"] = "Bearer presentation-time"
        repo = client.app.state.repo
        hour = "2026-10-15T16:00:00+00:00"
        client.portal.call(repo.db.tasks.insert_one, {"id": "timezone-task", "name": "时区任务", "ip": "192.0.2.80"})
        client.portal.call(repo.db.files.insert_one, {
            "id": "timezone-file", "taskId": "timezone-task", "nodeId": "test", "hour": hour,
            "status": "READY", "bytes": 1,
        })

        catalog = client.get("/api/v1/tasks/timezone-task/log-hours", params={"date": "2026-10-16"})
        assert catalog.status_code == 200
        assert catalog.json()["total"] == 1
        assert catalog.json()["items"][0]["hour"] == "2026-10-16T00:00:00+08:00"
        assert catalog.json()["items"][0]["hourId"] == hour

        download = client.post("/api/v1/downloads", headers={"Idempotency-Key": "timezone-download"}, json={
            "taskId": "timezone-task", "hourIds": [catalog.json()["items"][0]["hour"]], "allowPartial": False,
        })
        assert download.status_code == 202, download.text

        response = client.post("/api/v1/log-searches", headers={"Idempotency-Key": "timezone-search"}, json={
            "taskId": "timezone-task", "start": "2026-10-16T00:00:00+08:00", "end": "2026-10-16T01:00:00+08:00",
        })
        assert response.status_code == 202, response.text


def test_document_samples_and_opaque_metadata_are_not_rewritten():
    """嵌入的文档/扩展对象不属于接口业务时间，不按内部同名键猜测转换。"""
    from camera_logs.common.presentation_time import present_json_time
    original = {"start": "2026-10-16T00:00:00+00:00", "createdAt": "2026-10-16T00:00:00+00:00"}
    for key in ("requestExample", "responseExample", "schemas", "requestSchema", "metadata", "extensions"):
        assert present_json_time({key: original}) == {key: original}
    assert present_json_time(original)["createdAt"] == "2026-10-16T08:00:00+08:00"
