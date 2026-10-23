"""公开接口的显式响应字段合同。

历史路由大多没有 ``response_model``。这里维护经路由公开投影审阅后的
合同，禁止从 responseExample、数据库文档或单次运行结果推断字段。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from camera_logs.common.api_contracts.response_admin import (
    DASHBOARD,
    DISPLAY_SETTINGS,
    IP_POLICY,
    PLATFORM_SETTINGS,
    TOKEN,
    TOKEN_SECRET,
)
from camera_logs.common.api_contracts.response_events import EVENT_PAGE
from camera_logs.common.api_contracts.response_identity import COMMAND, OPERATION, USER
from camera_logs.common.api_contracts.response_logs import (
    DOWNLOAD_HISTORY,
    INDEX_STATUS,
    JOB,
    LOG_FILE,
    LOG_GAP,
    LOG_HOURS_PAGE,
    SEARCH_RESULTS_PAGE,
)
from camera_logs.common.api_contracts.response_nodes import NODE, NODE_CONFIG
from camera_logs.common.api_contracts.response_reference import REFERENCE
from camera_logs.common.api_contracts.response_resource_details import (
    AUTH_RECORD_PAGE,
    COREDUMP,
    EXPORT,
    METRIC_HISTORY,
    RESOURCE,
    RESOURCE_CREATED,
    RESOURCE_LIST_PAGE,
)
from camera_logs.common.api_contracts.response_schema import (
    array,
    batch_authentication_error_schema,
    error_schema,
    field,
    obj,
    page,
)
from camera_logs.common.api_contracts.response_tasks import COMMAND_HISTORY, COMMON, TASK, TEMPLATE
from camera_logs.common.log_frames import DataFrame, GapFrame, StatusFrame
from camera_logs.resource_metrics.models import ResourceMetricDates

ERROR = error_schema()
VALIDATION = error_schema(True)
BATCH_AUTHENTICATION_ERROR = batch_authentication_error_schema()


BINARY = obj("二进制下载响应头；响应正文是文件字节，不是 JSON。", {
    "Content-Type": field("string", "文件正文的 MIME 媒体类型，例如 application/octet-stream。"),
    "Content-Length": field("integer", "本次响应正文的字节数；范围下载时仅为该段长度，不是完整文件大小。"),
    "Content-Range": field("string", "206 响应实际返回的字节区间及文件总字节数，格式为 bytes 起点-终点/总长度；终点包含在内。"),
    "Accept-Ranges": field("string", "服务器支持的范围请求单位；bytes 表示可用 Range 请求按字节下载。"),
    "ETag": field("string", "当前文件版本的 HTTP 实体标记，可原样用于 If-Range，避免续传拼接不同版本。"),
    "Content-Disposition": field("string", "浏览器下载处置方式及建议文件名，通常为 attachment 并附带 filename 或 filename*。"),
}, ("Content-Type",))


AUTH = [(401, "认证失败。"), (403, "权限、对象所有权或 IP 策略不允许。"), (422, "路径、查询或 JSON 输入校验失败。"), (500, "未处理服务异常。")]
PUBLIC = [(422, "路径、查询或请求头校验失败。"), (500, "未处理服务异常。")]
MUTATE = AUTH + [(409, "版本、状态或幂等键冲突。")]
NOT_FOUND = AUTH + [(404, "目标不存在或对当前身份不可见。")]


def contract(kind: str, description: str, schema: dict[str, Any] | None, errors: list[tuple[int, str]], conditional: list[str] | None = None,
             error_schemas: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    """组合一个显式成功协议和已审阅的错误状态。"""
    error_schemas = error_schemas or {}
    value = {"kind": kind, "description": description, "schema": schema,
             "errors": [{"status": status, "when": when,
                         "schema": error_schemas.get(status, VALIDATION if status == 422 else ERROR)}
                        for status, when in errors], "errorSchema": ERROR}
    if conditional:
        value["conditional"] = conditional
    return value


def json(schema: dict[str, Any], errors: list[tuple[int, str]] = AUTH, conditional: list[str] | None = None,
         error_schemas: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    """建立 JSON 成功响应。"""
    return contract("json", "成功响应为 JSON。", schema, errors, conditional, error_schemas)


def _mapping() -> dict[str, dict[str, Any]]:
    """逐一登记所有 HTTP operation id；漏登记会在目录生成时失败。"""
    values: dict[str, dict[str, Any]] = {}
    def add(method: str, paths: tuple[str, ...], value: dict[str, Any]) -> None:
        for path in paths:
            values[f"{method} {path}"] = deepcopy(value)
    empty = lambda errors=AUTH: contract("empty", "成功时无响应正文。", None, errors)
    add("GET", ("/health",), json(obj("健康检查。", {"status": field("string", "健康状态。")}, ("status",)), []))
    add("GET", ("/metrics",), contract("text", "Prometheus exposition 文本。", field("string", "指标文本。"), []))
    add("GET", ("/api/v1/api-reference",), json(REFERENCE))
    add("POST", ("/api/v1/auth/login",), json(obj("登录结果。", {"user": USER}, ("user",)), PUBLIC + [(401, "用户名、密码或账号状态无效。"), (403, "请求来源不允许。"), (429, "登录尝试过于频繁。")]))
    add("POST", ("/api/v1/auth/logout",), empty())
    add("POST", ("/api/v1/auth/password",), json(obj("改密后的当前身份。", {"user": USER}, ("user",)), AUTH + [(400, "当前密码错误或新旧密码相同。")]))
    add("GET", ("/api/v1/auth/me",), json(obj("当前身份。", {"user": USER}, ("user",))))
    add("GET", ("/api/v1/users",), json(page(USER))); add("POST", ("/api/v1/users",), json(USER, MUTATE))
    add("PATCH", ("/api/v1/users/{identifier}",), json(USER, MUTATE + [(404, "用户不存在。")]))
    add("DELETE", ("/api/v1/users/{identifier}",), empty(MUTATE + [(404, "用户不存在。")]))
    add("POST", ("/api/v1/users/{identifier}/reset-password",), json(USER, MUTATE + [(404, "用户不存在。")]))
    summary = obj("用户摘要。", {"id": field("string", "用户标识。"), "username": field("string", "登录名。"), "displayName": field("string", "展示名。")}, ("id", "username", "displayName"))
    add("GET", ("/api/v1/users/creators", "/api/v1/users/share-targets"), json(page(summary)))
    add("GET", ("/api/v1/users/permissions",), json(obj("可授权权限。", {"scopes": array("权限枚举。", obj("权限项。", {"value": field("string", "权限值。"), "label": field("string", "中文名称。")}, ("value", "label")))}, ("scopes",))))
    add("GET", ("/api/v1/tasks",), json(page(TASK))); add("POST", ("/api/v1/tasks",), json(TASK, MUTATE))
    add("GET", ("/api/v1/tasks/{task_id}",), json(TASK, NOT_FOUND)); add("PATCH", ("/api/v1/tasks/{task_id}",), json(TASK, MUTATE + [(404, "任务不存在。")]))
    add("DELETE", ("/api/v1/tasks/{task_id}",), json(TASK, MUTATE + [(404, "任务不存在。")], ["成功码为 202；deletionState 表示连接回收状态，日志与归档仍按保留期可读。"] ))
    add("POST", ("/api/v1/tasks/{task_id}/start", "/api/v1/tasks/{task_id}/stop", "/api/v1/tasks/{task_id}/pause", "/api/v1/tasks/{task_id}/resume", "/api/v1/tasks/{task_id}/restart"), json(OPERATION, MUTATE + [(404, "任务不存在。")]))
    add("GET", ("/api/v1/operations/{identifier}",), json(OPERATION, NOT_FOUND))
    add("GET", ("/api/v1/resources",), json(RESOURCE_LIST_PAGE)); add("POST", ("/api/v1/resources",), json(RESOURCE, MUTATE))
    batch_errors = {422: {"oneOf": [VALIDATION, BATCH_AUTHENTICATION_ERROR],
                          "description": "输入校验失败，或设备认证失败的逐项结果。"}}
    add("POST", ("/api/v1/resources/batch",), json(obj("批量资源创建结果。", {"items": array("创建的资源。", RESOURCE_CREATED)}, ("items",)), MUTATE, error_schemas=batch_errors))
    add("GET", ("/api/v1/resources/{identifier}",), json(RESOURCE, NOT_FOUND)); add("PATCH", ("/api/v1/resources/{identifier}",), json(RESOURCE, MUTATE + [(404, "资源不存在。")]))
    add("DELETE", ("/api/v1/resources/{identifier}",), empty(MUTATE + [(404, "资源不存在。")]))
    recognised = obj("设备认证结果。", {"model": field("string", "设备型号。"), "subSerialNumber": field("string", "设备子序列号。"), "softwareVersion": field("string", "软件版本。")}, ())
    add("POST", ("/api/v1/resources/authenticate", "/api/v1/resources/{identifier}/authenticate"), json(recognised, MUTATE))
    add("POST", ("/api/v1/resources/{identifier}/credential",), json(obj("已授权设备凭据。", {"username": field("string", "设备用户名。"), "password": field("string", "设备密码。")}, ("username", "password")), NOT_FOUND))
    add("GET", ("/api/v1/resources/{identifier}/authentication-records",), json(AUTH_RECORD_PAGE, NOT_FOUND))
    owner_task = obj("当前监控任务。", {"id": field("string", "任务标识。"), "name": field("string", "任务名称。")}, ("id", "name"))
    add("GET", ("/api/v1/resources/{identifier}/coredump-monitor",), json(obj("Coredump 监控归属。", {"active": field("boolean", "是否启用。"), "ownerTask": {**owner_task, "nullable": True}, "mountStatus": field("string", "挂载状态。", nullable=True)}, ("active", "ownerTask", "mountStatus")), NOT_FOUND))
    add("GET", ("/api/v1/resources/{resource_id}/coredumps",), json(page(COREDUMP), NOT_FOUND, ["sourceState=STABLE 时，可直接用 id 下载稳定源文件，不必创建导出副本。"] ))
    add("GET", ("/api/v1/resources/{resource_id}/resource-metrics",), json(METRIC_HISTORY, NOT_FOUND))
    add("GET", ("/api/v1/resources/{resource_id}/resource-metric-dates",), json(ResourceMetricDates.model_json_schema(), NOT_FOUND))
    add("GET", ("/api/v1/common-commands", "/api/v1/admin/common-commands"), json(page(COMMON)))
    add("POST", ("/api/v1/admin/common-commands",), json(COMMON, MUTATE)); add("PATCH", ("/api/v1/admin/common-commands/{identifier}",), json(COMMON, MUTATE + [(404, "命令不存在。")]))
    add("DELETE", ("/api/v1/admin/common-commands/{identifier}",), empty(MUTATE + [(404, "命令不存在。")]))
    add("POST", ("/api/v1/admin/common-commands/batch",), json(obj("批量命令操作结果。", {"action": field("string", "ENABLE、DISABLE 或 DELETE。"), "affected": field("integer", "原子影响记录数。")}, ("action", "affected")), MUTATE + [(503, "事务结果未知，请以相同幂等键重试。")]))
    add("GET", ("/api/v1/command-templates",), json(page(TEMPLATE))); add("POST", ("/api/v1/command-templates",), json(TEMPLATE, MUTATE))
    add("GET", ("/api/v1/command-templates/{identifier}",), json(TEMPLATE, NOT_FOUND)); add("PATCH", ("/api/v1/command-templates/{identifier}",), json(TEMPLATE, MUTATE + [(404, "模板不存在。")]))
    add("DELETE", ("/api/v1/command-templates/{identifier}",), empty(MUTATE + [(404, "模板不存在。")]))
    add("POST", ("/api/v1/tasks/{task_id}/commands",), json(COMMAND, MUTATE + [(404, "任务不存在。")]))
    add("GET", ("/api/v1/commands/{identifier}",), json(COMMAND, NOT_FOUND)); add("GET", ("/api/v1/tasks/{task_id}/command-executions",), json(COMMAND_HISTORY, NOT_FOUND))
    add("POST", ("/api/v1/downloads", "/api/v1/log-searches"), json(JOB, MUTATE, ["FAILED、CANCELLED、EXPIRED 时出现 error/errorMessage；includeResults=false 时 results/files 省略。"] ))
    add("GET", ("/api/v1/downloads/{identifier}", "/api/v1/log-searches/{identifier}"), json(JOB, NOT_FOUND, ["includeResults=false 时省略 results/files。"] ))
    add("DELETE", ("/api/v1/downloads/{identifier}", "/api/v1/log-searches/{identifier}"), empty(MUTATE + [(404, "作业不存在。")]))
    add("GET", ("/api/v1/tasks/{task_id}/downloads",), json(page(DOWNLOAD_HISTORY), NOT_FOUND))
    add("GET", ("/api/v1/log-searches/{identifier}/results",), json(SEARCH_RESULTS_PAGE, NOT_FOUND))
    browser = obj("临时浏览会话。", {"url": field("string", "带临时授权的下载地址。"), "expiresInSeconds": field("integer", "链接有效秒数。")}, ("url", "expiresInSeconds"))
    add("POST", ("/api/v1/downloads/{identifier}/browser-session", "/api/v1/coredumps/{identifier}/browser-session", "/api/v1/coredump-exports/{identifier}/browser-session"), json(browser, NOT_FOUND))
    add("GET", ("/api/v1/downloads/{identifier}/content",), contract("binary", "下载产物字节流；Range 成功时为 206。", BINARY, PUBLIC + [(404, "制品不存在。"), (409, "制品尚未完成。"), (410, "制品已过期或清理。"), (416, "请求范围不可用。")]))
    add("GET", ("/api/v1/coredumps/{identifier}/content",), contract("binary", "稳定且仍存在的源 Coredump 字节流；Range 成功时为 206。", BINARY, PUBLIC + [(404, "文件不存在。"), (409, "源文件未稳定、不可读且无可用冻结副本。"), (416, "请求范围不可用。")], ["sourceState=STABLE 且 sourcePresent=true 时直接读取源文件。"] ))
    add("GET", ("/api/v1/coredump-exports/{identifier}/content",), contract("binary", "Coredump 导出制品字节流；Range 成功时为 206。", BINARY, PUBLIC + [(404, "导出不存在。"), (409, "导出尚未完成或已过期。"), (416, "请求范围不可用。")]))
    add("POST", ("/api/v1/coredump-exports",), json(EXPORT, MUTATE)); add("GET", ("/api/v1/coredump-exports/{identifier}",), json(EXPORT, NOT_FOUND)); add("DELETE", ("/api/v1/coredump-exports/{identifier}",), empty(MUTATE + [(404, "导出不存在。")]))
    add("GET", ("/api/v1/log-files/{identifier}",), json(LOG_FILE, NOT_FOUND)); add("GET", ("/api/v1/log-files/{identifier}/content",), json(obj("日志字节片段。", {"fileId": field("string", "文件标识。"), "sessionId": field("string", "会话标识。"), "data": field("string", "Base64 字节。"), "nextOffset": field("integer", "下一读取偏移。")}, ("fileId", "sessionId", "data", "nextOffset")), NOT_FOUND))
    add("GET", ("/api/v1/tasks/{task_id}/log-days",), json(obj("可用日志日期。", {"days": array("上海自然日。", field("string", "YYYY-MM-DD。"))}, ("days",)), NOT_FOUND))
    add("GET", ("/api/v1/tasks/{task_id}/log-hours",), json(LOG_HOURS_PAGE, NOT_FOUND)); add("GET", ("/api/v1/tasks/{task_id}/log-gap-catalog",), json(LOG_GAP, NOT_FOUND))
    add("GET", ("/api/v1/tasks/{task_id}/search-index-status",), json(INDEX_STATUS, NOT_FOUND))
    add("GET", ("/api/v1/dashboard",), json(DASHBOARD, AUTH, ["无 logs:read 时日志统计字段为 null；非管理员时 nodes 字段为 null。"] ))
    add("GET", ("/api/v1/nodes",), json(page(NODE)))
    add("GET", ("/api/v1/admin/nodes",), json(obj("节点配置与发现列表。", {"items": array("已登记或发现的节点配置。", NODE_CONFIG)}, ("items",)))); add("POST", ("/api/v1/admin/nodes",), json(NODE_CONFIG, MUTATE)); add("PATCH", ("/api/v1/admin/nodes/{node_id}",), json(NODE_CONFIG, MUTATE + [(404, "节点不存在。")]))
    add("DELETE", ("/api/v1/admin/nodes/{node_id}",), empty(MUTATE + [(404, "节点不存在。")]))
    add("POST", ("/api/v1/nodes/{node_id}/confirm-isolation",), json(obj("隔离确认。", {"nodeId": field("string", "节点标识。"), "status": field("string", "固定为 ISOLATED。")}, ("nodeId", "status")), MUTATE))
    add("GET", ("/api/v1/admin/ip-policy",), json(IP_POLICY)); add("PATCH", ("/api/v1/admin/ip-policy",), json(IP_POLICY, MUTATE))
    add("GET", ("/api/v1/platform-settings",), json(PLATFORM_SETTINGS)); add("GET", ("/api/v1/display-settings",), json(DISPLAY_SETTINGS)); add("PATCH", ("/api/v1/platform-settings",), json(PLATFORM_SETTINGS, MUTATE))
    add("GET", ("/api/v1/audit-events", "/api/v1/runtime-events", "/api/v1/request-events"), json(EVENT_PAGE))
    add("POST", ("/api/v1/service-tokens",), json(TOKEN_SECRET, MUTATE)); add("GET", ("/api/v1/service-tokens",), json(page(TOKEN)))
    add("PATCH", ("/api/v1/service-tokens/{identifier}",), json(TOKEN, MUTATE + [(404, "令牌不存在。")]))
    add("DELETE", ("/api/v1/service-tokens/{identifier}",), empty(MUTATE + [(404, "令牌不存在。")]))
    add("POST", ("/api/v1/service-tokens/{identifier}/reveal",), json(obj("令牌明文结果。", {"token": field("string", "仅本次展示的令牌明文。")}, ("token",)), MUTATE + [(404, "令牌不存在。")]))
    add("POST", ("/api/v1/service-tokens/{identifier}/rotate",), json(TOKEN_SECRET, MUTATE + [(404, "令牌不存在。")]))
    return values


CONTRACTS = _mapping()


def response_contract(path: str, method: str, status: int, example: Any = None, openapi_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回显式登记合同；未登记的新公开路由会阻止目录生成。"""
    del status, example, openapi_schema
    identifier = f"{method} {path}"
    if identifier == "WS /api/v1/tasks/{task_id}/logs":
        return contract("websocket", "先发送 {token?,cursor?}；服务端再推送 data、gap 或 status 帧。", {
            "oneOf": [DataFrame.model_json_schema(), GapFrame.model_json_schema(), StatusFrame.model_json_schema()],
        }, [], ["握手超时关闭 4408；认证 4401；授权 4403；服务上下文问题 1013；未处理异常 1011。"])
    try:
        return deepcopy(CONTRACTS[identifier])
    except KeyError as exc:
        raise RuntimeError(f"未登记公开操作的响应合同：{identifier}") from exc
