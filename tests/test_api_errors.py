"""验证实际错误正文由共享响应合同校验后再输出。"""

import json

import pytest
from camera_logs.common.api_contracts.response_schema import batch_authentication_error_schema
from camera_logs.common.api_errors import api_error_response, api_error_response_for_schema
from pydantic import ValidationError


def test_common_error_responses_follow_normal_and_validation_contracts():
    """普通错误与请求校验错误分别保留各自允许的 details 形状。"""
    normal = api_error_response(503, "SERVICE_NOT_READY", "服务正在初始化", None)
    validation = api_error_response(422, "VALIDATION_ERROR", "输入校验失败", "request-1",
                                    details=[{"location": ["query", "page"], "message": "无效"}])

    assert json.loads(normal.body) == {"error": {
        "code": "SERVICE_NOT_READY", "message": "服务正在初始化", "requestId": None,
    }}
    assert json.loads(validation.body) == {"error": {
        "code": "VALIDATION_ERROR", "message": "输入校验失败", "requestId": "request-1",
        "details": [{"location": ["query", "page"], "message": "无效"}],
    }}


def test_batch_authentication_error_rejects_a_non_business_details_shape():
    """业务认证失败只能输出逐项 index、IP 与安全失败类别。"""
    response = api_error_response_for_schema(
        422, "BATCH_AUTHENTICATION_FAILED", "批量设备认证失败", None,
        batch_authentication_error_schema(), details={"items": [{"index": 1, "ip": "192.0.2.82", "code": "AUTH_FAILED"}]},
    )
    assert b'BATCH_AUTHENTICATION_FAILED' in response.body
    with pytest.raises(ValidationError):
        api_error_response_for_schema(
            422, "BATCH_AUTHENTICATION_FAILED", "批量设备认证失败", None,
            batch_authentication_error_schema(), details=[{"location": ["body"], "message": "错误"}],
        )
