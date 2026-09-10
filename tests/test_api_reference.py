"""内置文档与实际路由清单一致，所有请求示例必须通过对应输入模型。"""
# ruff: noqa: F811
from fastapi.routing import APIRoute
from pydantic import TypeAdapter
from test_api import client  # noqa: F401


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


def test_reference_guides_document_inherited_permissions_owner_contract_and_template_sharing(client):
    """目录指南必须说明当前权限收窄、所有权和模板共享的实际合同。"""
    guides = "\n".join(item["text"] for item in client.get("/api/v1/api-reference").json()["guides"])

    for text in ("自动拥有", "来源IP", "仅创建者", "sharedWith", "sharedWithAll", "任务快照"):
        assert text in guides


def test_reference_coredumps_describe_binary_downloads_and_real_permissions(client):
    """coredump 不得继承资源编辑权限或被错误描述成日志 Base64 读取接口。"""
    reference = client.get("/api/v1/api-reference").json()
    operations = {item["id"]: item for item in reference["operations"]}
    listing = operations["GET /api/v1/resources/{resource_id}/coredumps"]
    assert listing["group"] == "Coredump文件"
    assert listing["permission"] == "logs:read"
    assert listing["responseExample"]["items"][0]["receivedAt"]
    create = operations["POST /api/v1/coredump-exports"]
    assert create["requestExample"] == {"fileIds": ["coredump-example"]}
    assert "Idempotency-Key" in create["headers"]
    for route in ("coredumps", "coredump-exports"):
        content = operations[f"GET /api/v1/{route}/{{identifier}}/content"]
        assert "logs:download" in content["permission"]
        assert "download_access" in content["headers"]["Authorization"]
        assert "二进制" in content["responseExample"]
    guides = "\n".join(item["text"] for item in reference["guides"])
    assert "WAITING_DEVICE" in guides and "新认证" in guides
