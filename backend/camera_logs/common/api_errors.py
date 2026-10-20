"""使用与 OpenAPI 相同的错误合同构造公共 HTTP 错误正文。"""

from collections.abc import Mapping
from typing import Any

from fastapi.responses import JSONResponse

from camera_logs.common.api_contracts.response_schema import error_schema
from camera_logs.common.response_models import response_model_from_schema


def api_error_response(status_code, code, message, request_id, *, details=None, headers=None):
    """按共享合同校验已脱敏错误，缺省校验明细不注入响应。

    脱敏由调用方完成；此处不保留原始异常或请求输入，避免日志和正文泄露凭据。
    """
    return api_error_response_for_schema(
        status_code, code, message, request_id, error_schema(details is not None), details=details, headers=headers,
    )


def api_error_response_for_schema(status_code: int, code: str, message: str, request_id: str | None,
                                  schema: Mapping[str, Any], *, details=None, headers=None):
    """按指定错误合同校验并生成 JSON 响应，供业务错误保留专有明细结构。"""
    error = {"code": code, "message": message, "requestId": request_id}
    if details is not None:
        error["details"] = details
    content = response_model_from_schema("ApiError", schema).model_validate({"error": error})
    return JSONResponse(status_code=status_code, headers=headers,
                        content=content.model_dump(mode="json", exclude_unset=True))
