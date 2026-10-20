"""验证 API 目录从 FastAPI 源声明动态生成，而非维护字段注册表。"""

from typing import Annotated

import pytest
from camera_logs.common.api_response_contracts import install_response_contracts
from camera_logs.common.response_models import StrictResponseModel
from camera_logs.reference.catalog import catalog
from fastapi import Body, FastAPI, Path, Query
from fastapi.exceptions import ResponseValidationError
from fastapi.testclient import TestClient
from pydantic import Field


class DynamicRequest(StrictResponseModel):
    """临时接口请求。"""

    name: Annotated[str, Field(description="采集任务展示名称。")]
    retryLimit: Annotated[int, Field(description="允许的重试次数。", ge=0)]


class DynamicResponse(StrictResponseModel):
    """临时接口响应。"""

    id: Annotated[str, Field(description="新建对象的稳定标识。")]
    revision: Annotated[int, Field(description="新增加的整型修订号。")]
    active: Annotated[bool, Field(description="对象是否已启用。")]


def _application() -> FastAPI:
    """创建仅由源码 Pydantic 声明构成的临时公开接口。"""
    app = FastAPI()

    @app.post("/api/v1/generated/{resource_id}", response_model=DynamicResponse)
    async def generated(
        resource_id: Annotated[str, Path(description="待写入的设备资源标识。")],
        trace: Annotated[str, Query(description="调用方追踪标识。")],
        body: Annotated[DynamicRequest, Body()],
        mode: Annotated[str, Query(description="测试返回形态。")] = "valid",
    ):
        if mode == "extra":
            return {"id": resource_id, "revision": 3, "active": True, "unexpected": trace}
        if mode == "wrong-type":
            return {"id": resource_id, "revision": "not-an-integer", "active": True}
        if mode == "missing":
            return {"id": resource_id, "active": True}
        return {"id": resource_id, "revision": 3, "active": body.retryLimit >= 0}

    install_response_contracts(app)
    return app


def _operation(app: FastAPI) -> tuple[dict, dict]:
    """取临时接口在动态目录中的唯一操作。"""
    reference = catalog(app)
    operation = next(item for item in reference["operations"] if item["id"] == "POST /api/v1/generated/{resource_id}")
    return operation, reference["schemas"]


def _resolve_schema(schema: dict, schemas: dict) -> dict:
    """按目录公开的 OpenAPI 引用取得声明模型，贴合前端的实际展示方式。"""
    if "$ref" not in schema:
        return schema
    return schemas[schema["$ref"].rsplit("/", 1)[-1]]


def test_catalog_automatically_reflects_declared_request_response_and_parameters():
    """新字段、类型和必填属性均来自临时源码模型，不依赖 reference 注册表。"""
    operation, schemas = _operation(_application())
    request = _resolve_schema(operation["requestSchema"], schemas)
    response = operation["responseContract"]["schema"]

    assert request["properties"]["retryLimit"]["type"] == "integer"
    assert request["properties"]["retryLimit"]["description"] == "允许的重试次数。"
    assert "retryLimit" in request["required"]
    assert response["properties"]["revision"]["type"] == "integer"
    assert response["properties"]["revision"]["description"] == "新增加的整型修订号。"
    assert {"id", "revision", "active"} <= set(response["required"])
    parameters = {item["name"]: item for item in operation["parameters"]}
    assert parameters["resource_id"]["description"] == "待写入的设备资源标识。"
    assert parameters["trace"]["description"] == "调用方追踪标识。"


@pytest.mark.parametrize("mode", ["extra", "wrong-type", "missing"])
def test_declared_strict_response_model_rejects_invalid_runtime_output(mode):
    """额外字段、类型错误和必填字段缺失都由实际 FastAPI 响应校验拒绝。"""
    client = TestClient(_application(), raise_server_exceptions=True)
    with pytest.raises(ResponseValidationError):
        client.post(
            f"/api/v1/generated/resource-contract?trace=trace&mode={mode}",
            json={"name": "动态合同", "retryLimit": 1},
        )


@pytest.mark.parametrize("description", [None, " ", "Identifier"])
def test_catalog_rejects_source_model_field_without_chinese_description(description):
    """遗漏 Field(description=...) 时目录生成失败，不能由独立字典兜底。"""
    app = FastAPI()

    class MissingDescription(StrictResponseModel):
        id: str = Field(description=description)

    @app.get("/api/v1/missing-description", response_model=MissingDescription)
    async def missing_description():
        return {"id": "missing"}

    install_response_contracts(app)
    with pytest.raises(RuntimeError, match="缺少中文说明"):
        catalog(app)
