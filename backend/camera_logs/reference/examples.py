"""接口文档的合成请求与响应示例；禁止读取实际账号、设备或运行配置。"""

STAMP = "2026-09-09T10:00:00+00:00"
TASK = {"id": "task-example", "resourceId": "resource-example", "name": "大厅日志", "protocol": "SSH",
        "ip": "192.0.2.10", "port": 22, "status": "STOPPED", "desiredState": "STOPPED",
        "createdBy": "user-example", "createdByName": "集成账号", "version": 1}
RESOURCE = {"id": "resource-example", "name": "大厅设备", "kind": "HIKVISION_NETWORK", "ip": "192.0.2.10",
            "model": "DS-2CD", "subSerialNumber": "SN-EXAMPLE", "softwareVersion": "V5.10 build 260612",
            "createdBy": "user-example", "createdByName": "集成账号",
            "taskCount": 1, "activeTaskCount": 0, "tasks": [TASK], "tasksTruncated": False,
            "tasksUrl": "/api/v1/tasks?resourceId=resource-example", "version": 1}
USER = {"id": "user-example", "username": "integration-user", "displayName": "集成账号", "isAdmin": False,
        "enabled": True, "scopes": ["tasks:read", "logs:read", "logs:download", "templates:read", "templates:write", "service-tokens:read"], "version": 1}
TEMPLATE = {"id": "template-example", "name": "日志模板", "description": "示例", "version": 1,
            "createdBy": "user-example", "createdByName": "集成账号", "sharedWith": ["shared-user-example"],
            "sharedWithAll": False, "initialCommands": [{"command": "ls"}], "scheduledCommands": []}
JOB = {"id": "job-example", "taskId": "task-example", "kind": "DOWNLOAD", "status": "QUEUED", "progress": 0}
COMMAND = {"id": "command-example", "taskId": "task-example", "kind": "MANUAL", "command": "ls", "status": "QUEUED"}
NODE = {"id": "collector-01", "url": "http://192.0.2.20:8001", "capacity": 100, "accepting": True,
        "version": 1, "registered": True, "online": True}
VALUES = {"name": "示例名称", "username": "integration-user", "password": "Example-password-123!",
          "currentPassword": "Example-current-123!", "newPassword": "Example-new-password-123!",
          "ip": "192.0.2.10", "port": 22, "resourceId": "resource-example", "taskId": "task-example",
          "nodeId": "collector-01", "url": "http://192.0.2.20:8001", "command": "ls",
          "confirmation": "CONFIRM_NODE_ISOLATED", "evidence": "管理员已通过基础设施关闭旧节点，工单INC-EXAMPLE",
          "scopes": ["tasks:read", "logs:read"], "network": "192.0.2.0/24", "displayName": "集成账号"}


def schema_example(schema, schemas, name="", depth=0):
    """按真实输入Schema递归构造示例，使用固定文档地址和虚拟凭据。"""
    if depth > 12:
        return None
    if "$ref" in schema:
        return schema_example(schemas[schema["$ref"].split("/")[-1]], schemas, name, depth + 1)
    if name in VALUES:
        return VALUES[name]
    if "default" in schema:
        return schema["default"]
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        return schema_example(next(item for item in schema["anyOf"] if item.get("type") != "null"), schemas, name, depth + 1)
    if schema.get("type") == "object":
        return {key: schema_example(value, schemas, key, depth + 1) for key, value in schema.get("properties", {}).items()}
    if schema.get("type") == "array":
        return [schema_example(schema.get("items", {}), schemas, depth=depth + 1)] if schema.get("minItems", 0) else []
    if schema.get("type") == "boolean":
        return False
    if schema.get("type") in ("integer", "number"):
        return max(1, schema.get("minimum", 1), schema.get("exclusiveMinimum", 0) + 1)
    if schema.get("format") in ("date-time", "date"):
        return STAMP if schema["format"] == "date-time" else "2026-09-09"
    return "example"


def request_example(schema, schemas):
    """在Schema默认值之上补齐有业务含义的任务、查询及下载示例。"""
    name = schema.get("$ref", "").split("/")[-1]
    if name == "SearchCreate":
        return {"taskId": "task-example", "start": STAMP, "end": "2026-09-09T11:00:00+00:00", "keyword": ""}
    if name == "DownloadCreate":
        return {"taskId": "task-example", "hourIds": [STAMP], "allowPartial": False}
    if name == "CoredumpExportCreate":
        return {"fileIds": ["coredump-example"]}
    if name == "TaskPatch":
        return {"version": 1, "name": "更新后的任务名称"}
    return schema_example(schema, schemas)


def page(item):
    return {"items": [item], "total": 1, "page": 1, "pageSize": 20}


def response_example(path, method, status):
    """返回代表性的成功响应字段；空响应和二进制使用明确的协议示例。"""
    if status == 204:
        return None
    if path == "/health":
        return {"status": "ok"}
    if path == "/metrics":
        return "# TYPE camera_tasks gauge\ncamera_tasks 2\n"
    if "coredump" in path:
        if path.endswith("/browser-session"):
            return {"url": path.removesuffix("/browser-session") + "/content", "expiresInSeconds": 300}
        if path.endswith("/content"):
            return "<二进制 coredump 或 ZIP STORE；支持 Range: bytes=0-1048575 和 If-Range>"
        if path.endswith("/coredumps"):
            return page({"id": "coredump-example", "resourceId": "resource-example", "nodeId": "collector-01",
                         "name": "core-example", "status": "RECEIVING", "size": 1048576,
                         "receivedAt": STAMP, "sourceModifiedAt": STAMP, "version": 1})
        return {"id": "coredump-export-example", "kind": "COREDUMP_EXPORT", "status": "QUEUED",
                "createdAt": STAMP, "expiresAt": "2026-09-10T10:00:00+00:00"}
    if path.endswith("/browser-session"):
        return {"url": "/api/v1/downloads/job-example/content", "expiresInSeconds": 300}
    if path.endswith("/content"):
        if "/downloads/" in path:
            return "<二进制 .tar.gz 或 .zip；支持 Range: bytes=0-1048575>"
        return {"fileId": "file-example", "sessionId": "session-example", "data": "bG9nCg==", "nextOffset": 4}
    if path.endswith("/results"):
        return page({"fileId": "file-example", "offset": 0, "length": 4, "receivedAt": STAMP,
                     "data": "bG9nCg==", "text": "log\n"}) | {"truncated": False, "status": "SUCCEEDED"}
    if path.endswith("/log-hours"):
        return page({"hourId": STAMP, "hour": STAMP, "status": "READY", "integrity": "VERIFIED",
                     "files": [{"id": "file-example", "bytes": 1024}], "bytes": 1024, "archiveBytes": 512})
    if path.endswith("/command-executions"):
        return page(COMMAND)
    if path.endswith("/users/permissions"):
        return {"scopes": [{"value": "tasks:read", "label": "查看设备与任务"}]}
    if path.endswith("/users/share-targets"):
        return page({"id": "shared-user-example", "username": "shared-user", "displayName": "可共享用户"})
    if "/commands" in path:
        return COMMAND
    if any(path.endswith("/" + action) for action in ("start", "stop", "pause", "resume")) or "/operations/" in path:
        return {"id": "operation-example", "taskId": "task-example", "status": "PENDING",
                "desiredState": "STOPPED" if path.endswith("/stop") else "PAUSED" if path.endswith("/pause") else "RUNNING"}
    if "/log-searches" in path or "/downloads" in path:
        return JOB | {"kind": "SEARCH" if "/log-searches" in path else "DOWNLOAD"}
    if "/resources" in path:
        if path.endswith("/authenticate"):
            return {key: RESOURCE[key] for key in ("model", "subSerialNumber", "softwareVersion")}
        return page(RESOURCE) if method == "GET" and path.endswith("/resources") else RESOURCE
    if "/command-templates" in path:
        return page(TEMPLATE) if method == "GET" and path.endswith("/command-templates") else TEMPLATE
    if "/tasks" in path:
        return page(TASK) if method == "GET" and path.endswith("/tasks") else TASK
    if "/auth/" in path:
        return {"user": USER}
    if path == "/api/v1/users/creators":
        return page({"id": "user-example", "username": "integration-user", "displayName": "集成账号"})
    if "/users" in path:
        return page(USER) if method == "GET" else USER
    if "/service-tokens" in path:
        if path.endswith("/reveal"):
            return {"token": "<SERVICE_TOKEN>"}
        token = {"id": "token-example", "name": "第三方集成", "userId": "user-example", "version": 1,
                 "revoked": False, "expiresAt": STAMP, "effectiveStatus": "ACTIVE",
                 "user": {"id": "user-example", "username": "integration", "displayName": "集成账号",
                          "enabled": True, "deletedAt": None}}
        if method == "GET":
            return page(token)
        return token if method == "PATCH" else token | {"token": "<SERVICE_TOKEN>"}
    if "/ip-policy" in path:
        return {"version": 1, "enabled": False, "rules": [], "clientIp": "192.0.2.100"}
    if "/platform-settings" in path:
        return {"retentionDays": 7, "version": 1, "updatedAt": STAMP}
    if "/nodes" in path:
        if path.endswith("/confirm-isolation"):
            return {"nodeId": "collector-01", "status": "ISOLATED"}
        return {"items": [NODE]} if method == "GET" else NODE
    if path.endswith("events"):
        return page({"id": "event-example", "createdAt": STAMP, "level": "INFO", "outcome": "SUCCEEDED",
                     "summary": "任务控制请求已受理", "taskId": "task-example", "requestId": "request-example"})
    return {"version": "1.0", "operations": []}
