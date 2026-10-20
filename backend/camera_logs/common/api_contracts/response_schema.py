"""API 响应合同的可复用 Schema 构造器。"""

from __future__ import annotations

from typing import Any


def field(kind: str, text: str, **extra: Any) -> dict[str, Any]:
    """创建有中文业务说明的公开字段。"""
    return {"type": kind, "description": text, **extra}


def obj(text: str, values: dict[str, dict[str, Any]], required: tuple[str, ...]) -> dict[str, Any]:
    """创建对象合同，required 只包含所有成功状态都存在的字段。"""
    return {"type": "object", "description": text, "properties": values, "required": list(required)}


def array(text: str, items: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """创建数组合同。"""
    return field("array", text, items=items, **extra)


def page(item: dict[str, Any]) -> dict[str, Any]:
    """创建标准页码分页包装。"""
    return obj("普通页码分页结果。", {
        "items": array("当前页记录。", item), "total": field("integer", "匹配记录总数。"),
        "page": field("integer", "当前页码，从 1 开始。"), "pageSize": field("integer", "本页最大记录数。"),
    }, ("items", "total", "page", "pageSize"))


def error_schema(details: bool = False) -> dict[str, Any]:
    """镜像 main.py 中全局异常处理器的实际 JSON 形态。"""
    properties = {
        "code": field("string", "稳定错误码或 HTTP 状态码文本。"),
        "message": field("string", "已脱敏的中文错误说明。"),
        "requestId": field("string", "本次 HTTP 请求的追踪编号，可用于关联服务访问日志；请求上下文尚未建立时为 null。", nullable=True),
    }
    if details:
        properties["details"] = array("输入校验问题，仅 422 返回。", obj("单项校验问题。", {
            "location": array("无效输入位置。", {"oneOf": [
                field("string", "字段名。"), field("integer", "数组索引。"),
            ], "description": "字段名或数组索引。"}),
            "message": field("string", "校验失败原因。"),
        }, ("location", "message")))
    required = ("code", "message", "requestId")
    return obj("统一错误响应。", {"error": obj("错误内容。", properties, required)}, ("error",))


def batch_authentication_error_schema() -> dict[str, Any]:
    """定义批量设备认证失败的逐项安全错误，不复用输入校验明细形状。"""
    details = obj("批量认证失败明细。", {
        "items": array("认证失败的输入项。", obj("单台设备认证失败项。", {
            "index": field("integer", "请求 items 数组中失败设备的从零开始索引。"),
            "ip": field("string", "认证失败设备的规范 IP 地址。"),
            "code": field("string", "安全失败类别：AUTH_FAILED、OFFLINE 或 DEVICE_ERROR。"),
        }, ("index", "ip", "code"))),
    }, ("items",))
    return obj("批量设备认证失败响应。", {"error": obj("错误内容。", {
        "code": field("string", "固定为 BATCH_AUTHENTICATION_FAILED。", const="BATCH_AUTHENTICATION_FAILED"),
        "message": field("string", "已脱敏的批量认证失败说明。"),
        "details": details,
        "requestId": field("string", "本次 HTTP 请求的追踪编号；请求上下文尚未建立时为 null。", nullable=True),
    }, ("code", "message", "details", "requestId"))}, ("error",))
