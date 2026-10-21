"""验证响应校验诊断在异常路径中保持脱敏且不干扰请求事件。"""

from types import SimpleNamespace

from camera_logs.common import response_validation
from camera_logs.common.config import Settings
from camera_logs.main import create_app
from cryptography.fernet import Fernet
from fastapi.exceptions import ResponseValidationError
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient
from pydantic import BaseModel, ConfigDict, ValidationError


class _DynamicKeyPayload(BaseModel):
    """同名静态字段用于验证动态字典键不能借全局字段名泄露。"""

    model_config = ConfigDict(extra="forbid")
    token: str
    metadata: dict[str, int]


def test_dynamic_key_matching_static_field_name_is_hidden():
    """metadata 内的 token 是动态键，即使根模型也有 token 字段仍必须隐藏。"""
    try:
        _DynamicKeyPayload.model_validate({"token": "valid", "metadata": {"token": "not-an-int"}})
    except ValidationError as error:  # Pydantic 负责生成与 FastAPI 相同形状的位置路径。
        validation = ResponseValidationError(error.errors())
    else:
        raise AssertionError("测试输入必须触发响应校验错误")

    result = response_validation.response_validation_details(
        validation, SimpleNamespace(scope={"route": SimpleNamespace(response_model=_DynamicKeyPayload)}),
    )

    assert result == {
        "count": 1,
        "truncated": False,
        "issues": [{"location": ["metadata", "[未声明字段或动态键]"], "type": "int_parsing"}],
    }


def test_diagnostic_schema_failure_keeps_request_event(tmp_path, monkeypatch):
    """诊断无法编译模型时降级为空，不得吞掉原始500对应的请求事件。"""
    class Payload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        count: int

    settings = Settings(
        _env_file=None,
        bootstrap_token="diagnostic-bootstrap",
        encryption_key=Fernet.generate_key().decode(),
        log_root=tmp_path,
        start_background=False,
    )
    app = create_app(settings, AsyncMongoMockClient().camera_logs)

    @app.get("/api/v1/diagnostic-failure", response_model=Payload)
    async def invalid_response():
        return {"count": "not-an-int"}

    monkeypatch.setattr(response_validation, "_response_schema", lambda _model: (_ for _ in ()).throw(RuntimeError()))
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/diagnostic-failure", headers={"X-Request-ID": "diagnostic-failure"})
        event = client.portal.call(app.state.repo.db.request_events.find_one, {"requestId": "diagnostic-failure"})

    assert response.status_code == 500
    assert event["httpStatus"] == 500
    assert event["errorType"] == "ResponseValidationError"
    assert event["responseValidation"] is None
