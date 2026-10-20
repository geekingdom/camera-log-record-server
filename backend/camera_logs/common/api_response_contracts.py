"""将公开响应合同安装到既有 FastAPI 路由。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from fastapi.datastructures import DefaultPlaceholder
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, create_model_field, request_response


def _operation_id(method: str, path: str) -> str:
    """生成与响应合同登记表一致的 HTTP 操作标识。"""
    return f"{method} {path}"


def _model_name(route: APIRoute, method: str) -> str:
    """构造稳定且可读的 Pydantic 模型名，避免路径字符进入类名。"""
    safe = re.sub(r"[^0-9A-Za-z_]", "_", route.unique_id)
    return f"ResponseContract_{method}_{safe}"


def _is_json_route(route: APIRoute) -> bool:
    """判断路由的成功正文是否由标准 JSON 响应类承载。"""
    response_class = route.response_class
    if isinstance(response_class, DefaultPlaceholder):
        response_class = response_class.value
    return isinstance(response_class, type) and issubclass(response_class, JSONResponse)


def _contract_extension(contract: Mapping[str, Any]) -> dict[str, Any]:
    """保留不属于 OpenAPI Schema 的状态条件和错误合同。"""
    if contract["kind"] != "json":
        return dict(contract)
    return {key: contract[key] for key in ("kind", "conditional", "errors") if key in contract}


def _install_error_responses(route: APIRoute, contract: Mapping[str, Any]) -> None:
    """把合同错误模型写入 OpenAPI，覆盖框架默认的泛化 422 描述。"""
    responses = dict(route.responses)
    for error in contract.get("errors", []):
        status = error["status"]
        previous = dict(responses.get(status, {}))
        content = dict(previous.get("content", {}))
        content["application/json"] = {"schema": error["schema"]}
        responses[status] = {
            **previous,
            "description": error["when"],
            "content": content,
        }
    route.responses = responses


def _attach_contract_metadata(route: APIRoute, contract: Mapping[str, Any]) -> None:
    """将条件与错误说明附加到 OpenAPI operation，不改变已有扩展字段。"""
    route.openapi_extra = {
        **(route.openapi_extra or {}),
        "x-response-contract": _contract_extension(contract),
    }


def _install_json_model(route: APIRoute, method: str, schema: Mapping[str, Any]) -> None:
    """安装生成模型并重建 ASGI 处理器，使缺失字段保持缺失。"""
    from camera_logs.common.api_contracts.response_reference import RESPONSE_SCHEMA
    from camera_logs.common.response_models import response_model_from_schema

    route.response_model = response_model_from_schema(
        _model_name(route, method),
        schema,
        {"ResponseSchema": RESPONSE_SCHEMA},
    )
    route.response_field = create_model_field(
        name=f"Response_{route.unique_id}",
        type_=route.response_model,
        mode="serialization",
    )
    # 合同只校验实际返回键；不能为旧文档或状态分支补出默认字段。
    route.response_model_exclude_unset = True
    route.app = request_response(route.get_route_handler())


def install_response_contracts(app) -> None:
    """把未声明模型的 JSON 路由绑定到合同，并拒绝漏登记的公开响应。

    已显式声明 ``response_model`` 的路由以源码模型为准，仍携带合同元数据。
    binary、text 和空响应不创建 Pydantic 模型，避免破坏流式或 204 语义。
    """
    from camera_logs.common.api_contracts.responses import CONTRACTS

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or ():
            identifier = _operation_id(method, route.path_format)
            contract = CONTRACTS.get(identifier)
            if contract is None:
                if route.response_model is None and _is_json_route(route):
                    raise RuntimeError(f"未登记公开 JSON 路由的响应合同：{identifier}")
                if route.response_model is not None:
                    _attach_contract_metadata(route, {
                        "kind": "json",
                        "description": "响应模型由路由声明。",
                        "errors": [],
                    })
                continue
            _attach_contract_metadata(route, contract)
            _install_error_responses(route, contract)
            if contract["kind"] != "json" or route.response_model is not None:
                continue
            _install_json_model(route, method, contract["schema"])
    # 合同在应用构建后安装，必须废弃可能已生成的 OpenAPI 缓存。
    app.openapi_schema = None
