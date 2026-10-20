"""验证响应合同安装器会驱动 FastAPI 的实际序列化行为。"""

from camera_logs.common.api_response_contracts import install_response_contracts
from fastapi import FastAPI
from fastapi.exceptions import ResponseValidationError
from fastapi.responses import Response
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient


def _route(app: FastAPI, path: str) -> APIRoute:
    """按路径取得已注册的 HTTP 路由。"""
    return next(route for route in app.routes if isinstance(route, APIRoute) and route.path == path)


def test_installer_validates_json_contracts_without_injecting_missing_fields(monkeypatch):
    """生成模型过滤未知键，但 exclude_unset 保留可选字段缺省状态。"""
    app = FastAPI()

    @app.get("/api/v1/contract-json")
    async def response():
        return {"id": "one"}

    monkeypatch.setattr(
        "camera_logs.common.api_contracts.responses.CONTRACTS",
        {"GET /api/v1/contract-json": {
            "kind": "json", "schema": {
                "type": "object", "description": "测试响应。", "properties": {
                    "id": {"type": "string", "description": "标识。"},
                    "optional": {"type": "string", "description": "可选字段。"},
                }, "required": ["id"],
            }, "errors": [{
                "status": 422, "when": "输入无效。", "schema": {
                    "type": "object", "description": "错误。", "properties": {
                        "error": {"type": "string", "description": "错误文本。"},
                    }, "required": ["error"],
                },
            }], "conditional": ["测试条件。"],
        }},
    )

    install_response_contracts(app)
    route = _route(app, "/api/v1/contract-json")
    assert route.response_model is not None
    assert route.response_model_exclude_unset is True
    assert route.openapi_extra["x-response-contract"]["conditional"] == ["测试条件。"]
    assert TestClient(app).get("/api/v1/contract-json").json() == {"id": "one"}
    response_schema = app.openapi()["paths"]["/api/v1/contract-json"]["get"]["responses"]["422"]
    assert response_schema["content"]["application/json"]["schema"]["properties"]["error"]


def test_installer_preserves_existing_models_and_non_json_contracts(monkeypatch):
    """既有源码模型、二进制和空响应均不会被错误替换。"""
    app = FastAPI()

    @app.get("/api/v1/declared", response_model=dict[str, str])
    async def declared():
        return {"id": "one"}

    @app.get("/api/v1/binary", response_class=Response)
    async def binary():
        return Response(b"body", media_type="application/octet-stream")

    monkeypatch.setattr(
        "camera_logs.common.api_contracts.responses.CONTRACTS",
        {
            "GET /api/v1/declared": {"kind": "json", "schema": {}, "errors": []},
            "GET /api/v1/binary": {"kind": "binary", "description": "二进制正文。", "schema": {"type": "object"}, "errors": []},
        },
    )

    original_model = _route(app, "/api/v1/declared").response_model
    install_response_contracts(app)
    assert _route(app, "/api/v1/declared").response_model is original_model
    binary_route = _route(app, "/api/v1/binary")
    assert binary_route.response_model is None
    assert binary_route.openapi_extra["x-response-contract"]["description"] == "二进制正文。"


def test_installer_rejects_unregistered_json_route(monkeypatch):
    """无显式模型的新 JSON 路由不能绕过响应字段维护。"""
    app = FastAPI()

    @app.get("/api/v1/unregistered")
    async def response():
        return {"id": "one"}

    monkeypatch.setattr("camera_logs.common.api_contracts.responses.CONTRACTS", {})
    try:
        install_response_contracts(app)
    except RuntimeError as error:
        assert "GET /api/v1/unregistered" in str(error)
    else:
        raise AssertionError("未登记 JSON 路由必须拒绝启动")


def test_installer_rebuilds_handler_and_rejects_missing_required_field(monkeypatch):
    """重建后的处理器必须执行合同验证，而不是只修改 OpenAPI 元数据。"""
    app = FastAPI()

    @app.get("/api/v1/invalid-response")
    async def response():
        return {}

    monkeypatch.setattr(
        "camera_logs.common.api_contracts.responses.CONTRACTS",
        {"GET /api/v1/invalid-response": {
            "kind": "json", "schema": {
                "type": "object", "description": "测试响应。", "properties": {
                    "id": {"type": "string", "description": "必填标识。"},
                }, "required": ["id"],
            }, "errors": [],
        }},
    )
    install_response_contracts(app)
    with TestClient(app, raise_server_exceptions=True) as client:
        try:
            client.get("/api/v1/invalid-response")
        except ResponseValidationError as error:
            assert "id" in str(error)
        else:
            raise AssertionError("缺少合同必填字段必须触发响应校验错误")
