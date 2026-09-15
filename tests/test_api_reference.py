"""内置文档与实际路由清单一致，所有请求示例必须通过对应输入模型。"""
# ruff: noqa: F811
from fastapi.routing import APIRoute
from pydantic import TypeAdapter
from test_api import client  # noqa: F401


def test_node_write_latency_limit_is_documented_with_valid_examples(client):
    """节点配置目录需解释毫秒阈值和准入边界，并提供可直接提交的示例。"""
    reference = client.get("/api/v1/api-reference").json()
    for name in ("NodeRegistration", "NodeConfigPatch"):
        field = reference["schemas"][name]["properties"]["writeLatencyLimitMs"]
        assert "毫秒" in field["description"]
        assert "200" in field["description"]
    operations = {item["id"]: item for item in reference["operations"]}
    assert operations["POST /api/v1/admin/nodes"]["requestExample"]["writeLatencyLimitMs"] == 200
    assert operations["PATCH /api/v1/admin/nodes/{node_id}"]["requestExample"]["writeLatencyLimitMs"] == 500
    assert operations["POST /api/v1/admin/nodes"]["responseExample"]["writeLatencyLimitMs"] == 200


def test_management_event_cursor_contract_is_documented(client):
    """站内目录必须解释空游标首屏、按需统计和保持旧调用的兼容边界。"""
    reference = client.get("/api/v1/api-reference").json()
    for path in ("audit-events", "runtime-events", "request-events"):
        operation = next(item for item in reference["operations"] if item["id"] == f"GET /api/v1/{path}")
        parameters = {item["name"]: item for item in operation["parameters"]}
        assert "游标" in parameters["cursor"]["description"]
        assert "总数" in parameters["includeTotal"]["description"]
    guide = next(item["text"] for item in reference["guides"] if item["title"] == "管理事件游标分页")
    for term in ("nextCursor", "hasMore", "includeTotal=true", "422", "快照"):
        assert term in guide


def test_reference_blocked_restart_permission_and_pending_contract(client):
    """恢复文档明确普通恢复与管理员隔离的边界，不能以202冒充采集成功。"""
    reference = client.get("/api/v1/api-reference").json()
    operation = next(item for item in reference["operations"] if item["id"] == "POST /api/v1/tasks/{task_id}/restart")
    assert operation["title"] == "重新启动等待隔离任务"
    assert "tasks:control" in operation["permission"] and "confirmIsolation=true" in operation["permission"]
    assert operation["requestExample"] == {"confirmIsolation": False}
    assert operation["responseStatus"] == 202
    assert operation["responseExample"]["status"] == "PENDING"
    guide = next(item["text"] for item in reference["guides"] if item["title"] == "等待隔离任务恢复")
    for term in ("ISOLATION_REQUIRED", "COLLECTING", "evidence", "activeTaskCount", "unsettledTaskCount"):
        assert term in guide


def test_reference_covers_every_public_http_route_and_websocket(client):
    response = client.get("/api/v1/api-reference")
    assert response.status_code == 200
    catalog = response.json()
    expected = {(method.upper(), path) for path, methods in client.app.openapi()["paths"].items() for method in methods}
    actual = {(item["method"], item["path"]) for item in catalog["operations"]}
    assert actual == expected | {("WS", "/api/v1/tasks/{task_id}/logs")}
    assert all(item["permission"] and item["title"] and item["responseStatus"] for item in catalog["operations"])
    by_id = {item["id"]: item for item in catalog["operations"]}
    for route in client.app.routes:
        if isinstance(route, APIRoute) and route.body_field:
            for method in route.methods:
                example = by_id[f"{method} {route.path}"]["requestExample"]
                TypeAdapter(route.body_field.field_info.annotation).validate_python(example)


def test_reference_has_real_chinese_descriptions_for_every_input_parameter_and_nested_field(client):
    """公开目录不得再把未描述字段交给前端猜测，嵌套命令也必须有业务语义。"""
    reference = client.get("/api/v1/api-reference").json()
    schemas = reference["schemas"]

    def resolve(schema):
        if "$ref" in schema:
            return resolve(schemas[schema["$ref"].rsplit("/", 1)[-1]])
        return schema

    def assert_described(schema):
        schema = resolve(schema)
        for name, field in schema.get("properties", {}).items():
            assert field.get("description"), f"missing description for request field {name}"
            assert_described(field)
        if isinstance(schema.get("items"), dict):
            assert_described(schema["items"])
        for key in ("anyOf", "oneOf", "allOf"):
            for member in schema.get(key, []):
                assert_described(member)

    for operation in reference["operations"]:
        for parameter in operation["parameters"]:
            assert parameter.get("description"), f"missing description for parameter {operation['id']} {parameter['name']}"
        if operation["requestSchema"]:
            assert_described(operation["requestSchema"])
    task_create = next(item for item in reference["operations"] if item["id"] == "POST /api/v1/tasks")
    assert task_create["requestSchema"]
    assert schemas["InitialCommand"]["properties"]["prompt"]["description"] == "命令发送后需要等待设备输出匹配的可选提示符。"
    ssh_target = schemas["TaskCreate"]["properties"]["sshTarget"]
    assert "SSH" in ssh_target["description"] and "从机" in ssh_target["description"]
    assert task_create["requestExample"]["sshTarget"] == "HOST"


def test_reference_requires_authentication_and_is_available_to_read_only_token(client):
    token = client.post("/api/v1/service-tokens", json={"name": "文档令牌", "userId": "builtin-admin"}).json()["token"]
    assert client.get("/api/v1/api-reference", headers={"Authorization": "Bearer " + token}).status_code == 200
    assert client.get("/api/v1/api-reference", headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_reference_documents_reusable_credentials_and_admin_only_rotation(client):
    """口令查看与管理员轮换不能在目录中混用权限或响应结构。"""
    operations = {item["id"]: item for item in client.get("/api/v1/api-reference").json()["operations"]}
    listing = operations["GET /api/v1/service-tokens"]
    reveal = operations["POST /api/v1/service-tokens/{identifier}/reveal"]
    rotate = operations["POST /api/v1/service-tokens/{identifier}/rotate"]
    assert "service-tokens:read" in listing["permission"] and "绑定用户本人" in reveal["permission"]
    assert reveal["responseExample"] == {"token": "<SERVICE_TOKEN>"}
    assert rotate["permission"] == "admin" and rotate["requestExample"]["version"] >= 1
    assert "token" not in listing["responseExample"]["items"][0]
    assert "token" not in operations["PATCH /api/v1/service-tokens/{identifier}"]["responseExample"]


def test_reference_documents_actual_session_and_download_authentication_paths(client):
    """登录、会话和下载票据的认证入口不能被笼统写成 Bearer Token。"""
    operations = {item["id"]: item for item in client.get("/api/v1/api-reference").json()["operations"]}

    login = operations["POST /api/v1/auth/login"]
    assert login["permission"] == "无需预先认证（仍受平台来源IP策略约束）"
    assert login["headers"] == {"X-Requested-With": "XMLHttpRequest", "Content-Type": "application/json"}

    me = operations["GET /api/v1/auth/me"]
    assert me["permission"] == "仅Cookie登录会话"
    assert me["headers"] == {"Cookie": "camera_session=<登录会话Cookie>"}

    content = operations["GET /api/v1/downloads/{identifier}/content"]
    assert content["permission"] == "logs:download"
    assert "download_access" in content["headers"]["Authorization"]

    share_targets = operations["GET /api/v1/users/share-targets"]
    assert share_targets["permission"] == "templates:read"
    assert share_targets["responseExample"] == {
        "items": [{"id": "shared-user-example", "username": "shared-user", "displayName": "可共享用户"}],
        "total": 1,
        "page": 1,
        "pageSize": 20,
    }


def test_reference_distinguishes_saved_credential_authentication_from_editor_preview(client):
    """资源行空请求认证会改变健康状态，编辑器完整输入预览不得被误解为同一操作。"""
    operations = {item["id"]: item for item in client.get("/api/v1/api-reference").json()["operations"]}
    authentication = operations["POST /api/v1/resources/{identifier}/authenticate"]
    for text in ("空请求体", "已保存", "健康状态", "停止关联任务", "完整 ResourceInput", "预览"):
        assert text in authentication["description"]
    for text in ("resources:write", "tasks:control", "他人关联任务", "完整ResourceInput仅预览"):
        assert text in authentication["permission"]
    assert operations["POST /api/v1/resources/authenticate"]["permission"] == "resources:create"


def test_reference_response_examples_preserve_route_specific_contracts(client):
    """代表字段可省略，但不能用另一类端点的响应结构替代当前路由。"""
    operations = {item["id"]: item for item in client.get("/api/v1/api-reference").json()["operations"]}

    assert operations["GET /api/v1/users/permissions"]["responseExample"] == {
        "scopes": [{"value": "tasks:read", "label": "查看设备与任务"}]
    }
    assert operations["POST /api/v1/auth/logout"]["responseStatus"] == 204
    assert operations["POST /api/v1/auth/logout"]["responseExample"] is None
    assert operations["GET /api/v1/log-files/{identifier}/content"]["responseExample"] == {
        "fileId": "file-example", "sessionId": "session-example", "data": "bG9nCg==", "nextOffset": 4
    }

    websocket = operations["WS /api/v1/tasks/{task_id}/logs"]
    assert all(value in websocket["description"] for value in ("HTTP", "ws://", "HTTPS", "wss://"))

    user = operations["GET /api/v1/users"]["responseExample"]["items"][0]
    assert "resourceIds" not in user
    assert {"tasks:read", "logs:read", "logs:download", "templates:read", "templates:write"} <= set(user["scopes"])

    template = operations["GET /api/v1/command-templates"]["responseExample"]["items"][0]
    assert template["createdBy"] == "user-example"
    assert template["sharedWith"] == ["shared-user-example"]
    assert template["sharedWithAll"] is False

    task = operations["GET /api/v1/tasks"]["responseExample"]["items"][0]
    resource = operations["GET /api/v1/resources"]["responseExample"]["items"][0]
    assert task["createdBy"] == resource["createdBy"] == "user-example"
    assert task["sshTarget"] == "HOST"


def test_reference_guides_document_inherited_permissions_owner_contract_and_template_sharing(client):
    """目录指南必须说明当前权限收窄、所有权和模板共享的实际合同。"""
    guides = "\n".join(item["text"] for item in client.get("/api/v1/api-reference").json()["guides"])

    for text in ("自动拥有", "来源IP", "仅创建者", "sharedWith", "sharedWithAll", "任务快照"):
        assert text in guides


def test_reference_coredumps_describe_binary_downloads_and_real_permissions(client):
    """coredump 不得继承资源编辑权限或被错误描述成日志 Base64 读取接口。"""
    reference = client.get("/api/v1/api-reference").json()
    default_access = next(item["text"] for item in reference["guides"] if item["title"] == "Coredump默认访问权限")
    assert "默认允许Coredump查询、导出和下载" in default_access
    assert "logs:read" in default_access and "logs:download" in default_access
    assert "来源IP策略" in default_access and "绑定用户禁用" in default_access
    operations = {item["id"]: item for item in reference["operations"]}
    listing = operations["GET /api/v1/resources/{resource_id}/coredumps"]
    assert listing["group"] == "Coredump文件"
    assert listing["permission"] == "logs:read"
    assert listing["responseExample"]["items"][0]["receivedAt"]
    assert listing["responseExample"]["items"][0]["sourceState"] == "OBSERVING"
    assert listing["responseExample"]["items"][0]["sourceStableAt"] is None
    monitor = operations["GET /api/v1/resources/{identifier}/coredump-monitor"]
    assert monitor["responseExample"] == {
        "active": True, "ownerTask": {"id": "task-example", "name": "值守采集"}, "mountStatus": "MOUNTED",
    }
    create = operations["POST /api/v1/coredump-exports"]
    assert create["requestExample"] == {"fileIds": ["coredump-example"]}
    assert "Idempotency-Key" in create["headers"]
    for route in ("coredumps", "coredump-exports"):
        content = operations[f"GET /api/v1/{route}/{{identifier}}/content"]
        assert "logs:download" in content["permission"]
        assert "download_access" in content["headers"]["Authorization"]
        assert "二进制" in content["responseExample"]
    guides = "\n".join(item["text"] for item in reference["guides"])
    for text in ("WAITING_DEVICE", "新认证", "coredump_flag.cdf", "首次扫描观测时间", "sourceState", "设备时间影响"):
        assert text in guides
