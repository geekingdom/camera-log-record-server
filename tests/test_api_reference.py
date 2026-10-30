"""内置文档与实际路由清单一致，所有请求示例必须通过对应输入模型。"""
# ruff: noqa: F811
from fastapi.routing import APIRoute
from pydantic import TypeAdapter
from test_api import client  # noqa: F401


def test_resource_batch_contract_and_optional_creation_name_are_documented(client):
    """批量接口必须展示幂等头、原子语义和可校验的请求示例。"""
    catalog = client.get("/api/v1/api-reference").json()
    operation = next(item for item in catalog["operations"] if item["id"] == "POST /api/v1/resources/batch")
    assert operation["title"] == "批量创建设备资源"
    assert "Idempotency-Key" in operation["headers"]
    assert "resources:create" in operation["permission"]
    assert "原子" in operation["description"]
    assert operation["requestExample"]["items"][0]["kind"] == "HIKVISION_NETWORK"
    assert set(operation["responseExample"]) == {"items"}
    resource = catalog["schemas"]["ResourceInput"]
    assert "name" not in resource.get("required", [])
    assert "IP" in resource["properties"]["name"]["description"]


def test_credentials_sorting_and_progressive_search_are_documented(client):
    """新凭据权限、全局排序与渐进结果的完整性边界必须在站内目录一致展示。"""
    catalog = client.get("/api/v1/api-reference").json()
    operations = {item["id"]: item for item in catalog["operations"]}
    credential = operations["POST /api/v1/resources/{identifier}/credential"]
    assert "resources:write" in credential["permission"]
    assert "创建者、admin或指定共享用户" in credential["permission"]
    assert set(credential["responseExample"]) == {"username", "password"}
    for path in ("resources", "tasks"):
        parameters = {item["name"]: item for item in operations[f"GET /api/v1/{path}"]["parameters"]}
        assert "分页" in parameters["sortBy"]["description"]
    parameters = {item["name"]: item for item in operations["GET /api/v1/log-searches/{identifier}"]["parameters"]}
    assert "轮询" in parameters["includeResults"]["description"]
    guide = next(item["text"] for item in catalog["guides"] if item["title"] == "渐进式历史检索")
    assert "已扫描部分" in guide and "truncated=false" in guide


def test_node_write_latency_limit_is_documented_with_valid_examples(client):
    """节点配置目录需解释毫秒阈值和准入边界，并提供可直接提交的示例。"""
    reference = client.get("/api/v1/api-reference").json()
    for name in ("NodeRegistration", "NodeConfigPatch"):
        field = reference["schemas"][name]["properties"]["writeLatencyLimitMs"]
        assert "毫秒" in field["description"]
        assert "200" in field["description"]
        assert "最差会话" in field["description"]
        assert "少数慢会话" in field["description"]
        assert "旧节点" in field["description"]
    operations = {item["id"]: item for item in reference["operations"]}
    assert operations["POST /api/v1/admin/nodes"]["requestExample"]["writeLatencyLimitMs"] == 200
    assert operations["PATCH /api/v1/admin/nodes/{node_id}"]["requestExample"]["writeLatencyLimitMs"] == 500
    assert operations["POST /api/v1/admin/nodes"]["responseExample"]["writeLatencyLimitMs"] == 200


def test_platform_log_segment_size_is_documented_with_a_valid_example(client):
    """平台日志分片大小在输入模型、范围约束和响应示例中必须一致。"""
    reference = client.get("/api/v1/api-reference").json()
    operations = {item["id"]: item for item in reference["operations"]}
    field = reference["schemas"]["PlatformSettingsPatch"]["properties"]["logSegmentMiB"]
    numeric = next(item for item in field["anyOf"] if item.get("type") == "integer")
    assert numeric["minimum"] == 10 and numeric["maximum"] == 512
    assert "10至510" in field["description"] and "或512" in field["description"]
    assert "Worker每秒刷新，刷新成功后新分片生效" in field["description"]
    assert "已打开分片按原上限收尾" in field["description"]
    assert "下载作业启动时固定" in field["description"]
    update = operations["PATCH /api/v1/platform-settings"]
    assert update["requestExample"]["logSegmentMiB"] == 10
    assert update["responseExample"]["logSegmentMiB"] == 10


def test_node_performance_policy_is_documented_with_valid_nested_examples(client):
    """节点性能策略的范围、默认值和 PATCH 全量替换示例必须可直接提交。"""
    reference = client.get("/api/v1/api-reference").json()
    policy = reference["schemas"]["PerformancePolicy"]["properties"]
    assert policy["mode"]["default"] == "AUTO"
    assert policy["bufferMiB"]["minimum"] == 16 and policy["bufferMiB"]["maximum"] == 4096
    assert policy["writeConcurrency"]["minimum"] == 1 and policy["writeConcurrency"]["maximum"] == 32
    operations = {item["id"]: item for item in reference["operations"]}
    assert operations["POST /api/v1/admin/nodes"]["requestExample"]["performance"]["mode"] == "AUTO"
    assert operations["PATCH /api/v1/admin/nodes/{node_id}"]["requestExample"]["performance"]["mode"] == "MANUAL"
    recommendation = operations["GET /api/v1/admin/nodes"]["responseExample"]["items"][0]["performanceRecommendation"]
    assert recommendation["available"] and recommendation["resources"]["effectiveCpu"] == 4
    assert recommendation["policy"]["readMiBPerSecond"] == 25


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


def test_reference_documents_dashboard_permissions_and_response_availability(client):
    """看板目录必须明确任务读取基线与日志、管理员两层可用性，不能退化为通用查询。"""
    reference = client.get("/api/v1/api-reference").json()
    operations = {item["id"]: item for item in reference["operations"]}
    dashboard = operations["GET /api/v1/dashboard"]
    assert reference["version"] == "1.1"
    assert dashboard["title"] == "资源运行看板"
    assert dashboard["group"] == "资源看板"
    for text in ("tasks:read", "logs:read", "admin"):
        assert text in dashboard["permission"]
    for text in ("7、14或30", "null", "503", "原始分卷bytes"):
        assert text in dashboard["description"]
    assert dashboard["responseExample"]["availability"] == {"logs": True, "nodes": True}
    guide = next(item["text"] for item in reference["guides"] if item["title"] == "资源看板")
    for text in ("availability.logs=false", "archiveBytes", "accepting", "503"):
        assert text in guide


def test_reference_documents_authentication_merging_recovery_and_shell_boundaries(client):
    """认证合并、401锁存和主从保活是运行契约，必须随文档发布。"""
    reference = client.get("/api/v1/api-reference").json()
    guides = {item["title"]: item["text"] for item in reference["guides"]}
    history = guides["设备认证历史"]
    recovery = guides["认证失败与任务恢复"]
    shell = guides["SSH主从初始化与会话保活"]
    for text in ("AUTH_FAILED", "OFFLINE", "ERROR", "occurrenceCount", "人工认证"):
        assert text in history
    for text in ("401", "锁存", "authRejectedAt", "周期认证", "SUCCESS", "OFFLINE", "AUTH_FAILED", "STOPPED", "10秒", "Telnet串口"):
        assert text in recovery
    assert "只有保存凭据后成功的人工认证才能授权恢复" not in recovery
    assert "用户停止或暂停的任务" in recovery and "不会自动重启" in recovery
    for text in ("SLAVE_1", "独立确认", "已验证的SSH从机", "60秒", "TELNET_SERIAL", "发送队列"):
        assert text in shell
    assert "sshTarget=HOST" in guides["设备CPU与内存监控"]
    schemas = reference["schemas"]
    assert "唯一发送队列" in schemas["TaskCreate"]["properties"]["initialCommands"]["description"]
    assert "不阻塞采集" in schemas["TaskCreate"]["properties"]["initialCommands"]["description"]
    assert "独立确认" in schemas["TaskCreate"]["properties"]["sshTarget"]["description"]


def test_reference_documents_search_cancellation_ownership_and_protocol_response_types(client):
    """相近下载和检索作业的取消权限及JSON/二进制内容端点不能混写。"""
    operations = {item["id"]: item for item in client.get("/api/v1/api-reference").json()["operations"]}
    assert operations["DELETE /api/v1/log-searches/{identifier}"]["permission"] == "logs:read（取消本人作业；他人作业另需admin）"
    assert operations["GET /api/v1/log-files/{identifier}/content"]["responseExample"] == {
        "fileId": "file-example", "sessionId": "session-example", "data": "bG9nCg==", "nextOffset": 4,
    }
    assert "二进制" in operations["GET /api/v1/downloads/{identifier}/content"]["responseExample"]


def test_reference_uses_explicit_titles_for_history_search_and_download_workflows(client):
    """异步检索与下载不能因同属日志查询而在目录中得到相同模糊标题。"""
    operations = {item["id"]: item for item in client.get("/api/v1/api-reference").json()["operations"]}
    assert operations["POST /api/v1/log-searches"]["title"] == "创建历史检索作业"
    assert operations["GET /api/v1/log-searches/{identifier}"]["title"] == "查询历史检索作业状态"
    assert operations["GET /api/v1/log-searches/{identifier}/results"]["title"] == "分页读取历史检索结果"
    assert operations["DELETE /api/v1/log-searches/{identifier}"]["title"] == "取消历史检索作业"
    history = operations["GET /api/v1/tasks/{task_id}/downloads"]
    assert history["title"] == "分页读取任务下载历史"
    assert history["permission"] == "logs:download"
    assert history["responseExample"]["items"][0]["downloadAvailable"] is True


def test_reference_documents_search_index_coverage_status(client):
    """索引覆盖接口必须表达原文回退，不能把候选索引写成检索结果来源。"""
    operations = {item["id"]: item for item in client.get("/api/v1/api-reference").json()["operations"]}
    status = operations["GET /api/v1/tasks/{task_id}/search-index-status"]
    assert status["title"] == "查询历史检索索引覆盖状态"
    assert status["group"] == "日志查询"
    assert status["permission"] == "logs:read"
    assert set(status["responseExample"]) == {
        "enabled", "status", "totalFiles", "coveredFiles", "coverageComplete",
        "latestIndexedHour", "contiguousCoveredThrough", "truncated", "fallback",
    }
    assert status["responseExample"]["fallback"] == "RAW_SCAN"
    assert "原始日志扫描" in status["description"]


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


def test_reference_documents_minimal_live_log_display_settings(client):
    """实时页面配置应公开给有效身份，但目录不能误写为管理员完整后台设置。"""
    operations = {item["id"]: item for item in client.get("/api/v1/api-reference").json()["operations"]}
    display = operations["GET /api/v1/display-settings"]
    assert display["title"] == "查询实时日志显示配置"
    assert display["group"] == "平台配置"
    assert display["permission"] == "有效Token或登录会话（仅返回实时日志内存上限）"
    assert display["responseExample"] == {"liveLogBufferMiB": 10}
    guide = next(item["text"] for item in client.get("/api/v1/api-reference").json()["guides"]
                 if item["title"] == "实时日志显示配置")
    for text in ("liveLogBufferMiB", "1至100", "不返回", "Worker原始日志"):
        assert text in guide


def test_reference_documents_reusable_credentials_and_admin_only_rotation(client):
    """口令查看与管理员轮换不能在目录中混用权限或响应结构。"""
    operations = {item["id"]: item for item in client.get("/api/v1/api-reference").json()["operations"]}
    listing = operations["GET /api/v1/service-tokens"]
    reveal = operations["POST /api/v1/service-tokens/{identifier}/reveal"]
    rotate = operations["POST /api/v1/service-tokens/{identifier}/rotate"]
    assert "service-tokens:read" in listing["permission"] and "绑定用户本人" in reveal["permission"]
    assert reveal["responseExample"] == {"token": "<SERVICE_TOKEN>"}
    assert rotate["permission"].startswith("admin") and "其他管理员" in rotate["permission"]
    assert rotate["requestExample"]["version"] >= 1
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
    assert "公开附件" in content["permission"]
    assert "Authorization" not in content["headers"]

    share_targets = operations["GET /api/v1/users/share-targets"]
    assert share_targets["permission"] == "templates:read、resources:create、resources:write 或 admin（满足任一即可）"
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
    for text in ("空请求体", "已保存", "健康状态", "不停止仍有日志输出", "持续无输出", "Telnet串口不受认证状态影响", "完整 ResourceInput", "预览"):
        assert text in authentication["description"]
    for text in ("resources:write", "tasks:control", "指定共享用户", "关联任务归属限制", "完整ResourceInput仅预览"):
        assert text in authentication["permission"]
    assert operations["POST /api/v1/resources/authenticate"]["permission"] == "resources:create"


def test_reference_response_examples_preserve_route_specific_contracts(client):
    """代表字段可省略，但不能用另一类端点的响应结构替代当前路由。"""
    operations = {item["id"]: item for item in client.get("/api/v1/api-reference").json()["operations"]}

    assert operations["GET /api/v1/users/permissions"]["responseExample"] == {
        "scopes": [{"value": "tasks:read", "label": "查看设备与任务", "fixed": True}]
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
    assert "来源IP策略" in default_access and "无需登录" in default_access
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
        assert "公开附件" in content["permission"]
        assert "Authorization" not in content["headers"]
        assert "二进制" in content["responseExample"]
    guides = "\n".join(item["text"] for item in reference["guides"])
    for text in ("WAITING_DEVICE", "新认证", "coredump_flag.cdf", "首次扫描观测时间", "sourceState", "设备时间影响"):
        assert text in guides


def test_operation_names_distinguish_lists_details_states_and_downloads():
    """全模块名称不能沿用分组名拼接，避免把取消作业误称删除文件。"""
    from camera_logs.reference.catalog import operation_title
    expected = {
        ('GET', '/api/v1/log-files/{identifier}'): '查询日志分卷元数据与归档状态',
        ('GET', '/api/v1/audit-events'): '查询业务操作审计记录',
        ('GET', '/api/v1/runtime-events'): '查询采集运行事件',
        ('GET', '/api/v1/request-events'): '查询HTTP请求访问记录',
        ('DELETE', '/api/v1/coredump-exports/{identifier}'): '取消Coredump导出作业',
        ('GET', '/api/v1/downloads/{identifier}/content'): '下载已生成的日志归档文件',
        ('POST', '/api/v1/tasks/{task_id}/commands'): '向采集会话发送手动命令',
    }
    for key, title in expected.items():
        assert operation_title(*key) == title
    for route, identifier in (('tasks', 'task_id'), ('resources', 'identifier'), ('command-templates', 'identifier')):
        assert operation_title('GET', f'/api/v1/{route}') != operation_title('GET', f'/api/v1/{route}/{{{identifier}}}')


def test_time_inputs_and_hour_range_download_are_documented_without_required_offset(client):
    """输入示例直接使用北京时间，下载范围合同和稳定源直链在目录明确可见。"""
    from datetime import datetime

    reference = client.get("/api/v1/api-reference").json()
    operations = {item["id"]: item for item in reference["operations"]}
    for operation in operations.values():
        for parameter in operation["parameters"]:
            if parameter["name"] in {"start", "end", "receivedFrom", "receivedTo"}:
                assert datetime.fromisoformat(parameter["example"]).tzinfo is None
                assert "北京时间" in parameter["description"]
    for name in ("POST /api/v1/log-searches", "POST /api/v1/downloads"):
        example = operations[name]["requestExample"]
        assert datetime.fromisoformat(example["start"]).tzinfo is None
        assert datetime.fromisoformat(example["end"]).tzinfo is None
    download = operations["POST /api/v1/downloads"]
    assert "hourIds" in download["description"] and "向上取整" in download["description"]
    core = operations["GET /api/v1/coredumps/{identifier}/content"]
    assert "稳定" in core["description"] and "无需先导出" in core["description"]


def test_resource_metric_calendar_dates_are_documented(client):
    """日期选择器接口的月份、权限、软删保留与精简响应必须来自实际路由合同。"""
    reference = client.get("/api/v1/api-reference").json()
    operations = {item["id"]: item for item in reference["operations"]}
    operation = operations["GET /api/v1/resources/{resource_id}/resource-metric-dates"]
    assert operation["title"] == "查询资源指标可用日期"
    assert operation["permission"] == "tasks:read"
    month = next(item for item in operation["parameters"] if item["name"] == "month")
    assert "上海" in month["description"] and "YYYY-MM" in month["description"]
    assert operation["responseExample"] == {"days": ["2026-10-19", "2026-10-20"]}
    days = operation["responseContract"]["schema"]["properties"]["days"]
    assert days["items"]["description"] == "上海自然日，格式 YYYY-MM-DD。"
    guide = next(item["text"] for item in reference["guides"] if item["title"] == "设备CPU与内存监控")
    for term in ("resource-metric-dates", "软删除", "不返回指标正文", "无数据日期省略"):
        assert term in guide


def test_reference_exposes_complete_response_field_contracts_and_protocol_variants(client):
    """每个公开操作都必须提供可递归展示的成功、错误及协议响应合同。"""
    reference = client.get("/api/v1/api-reference").json()
    for operation in reference["operations"]:
        contract = operation["responseContract"]
        assert contract["kind"] in {"json", "empty", "binary", "text", "websocket"}
        assert "errors" in contract
        if contract["kind"] in {"empty", "websocket"}:
            continue
        assert contract["schema"] and contract["schema"].get("description"), operation["id"]
    downloads = next(item for item in reference["operations"] if item["id"] == "GET /api/v1/downloads/{identifier}/content")
    assert downloads["responseContract"]["kind"] == "binary"
    assert set(downloads["responseContract"]["schema"]["properties"]) >= {"Content-Range", "ETag"}
    websocket = next(item for item in reference["operations"] if item["id"] == "WS /api/v1/tasks/{task_id}/logs")
    assert len(websocket["responseContract"]["schema"]["oneOf"]) == 3
    events = next(item for item in reference["operations"] if item["id"] == "GET /api/v1/audit-events")
    assert events["responseContract"]["schema"]["properties"]["items"]["items"]["properties"]["summary"]["description"]


def test_response_contracts_are_explicit_for_every_route_and_cover_stateful_protocols(client):
    """目录必须来自共享合同或路由显式响应模型，不能回退到示例推断。"""
    from camera_logs.common.api_contracts.responses import CONTRACTS

    reference = client.get("/api/v1/api-reference").json()
    operations = {operation["id"]: operation for operation in reference["operations"]}
    http_ids = {identifier for identifier in operations if not identifier.startswith("WS ")}
    explicit_models = {"GET /api/v1/resources/{resource_id}/resource-metric-dates"}
    assert set(CONTRACTS) | explicit_models == http_ids

    job = operations["POST /api/v1/downloads"]["responseContract"]
    assert job["kind"] == "json"
    assert {"hourIds", "files", "error", "errorMessage"} <= set(job["schema"]["properties"])
    assert any("includeResults=false" in note for note in job["conditional"])

    coredump = operations["GET /api/v1/coredumps/{identifier}/content"]["responseContract"]
    assert coredump["kind"] == "binary"
    assert {error["status"] for error in coredump["errors"]} == {404, 409, 416, 422, 500}
    assert "sourceState=STABLE" in coredump["conditional"][0]

    batch = operations["POST /api/v1/admin/common-commands/batch"]["responseContract"]["schema"]
    assert batch["required"] == ["action", "affected"]
    assert batch["properties"]["affected"]["description"]

    websocket = operations["WS /api/v1/tasks/{task_id}/logs"]["responseContract"]
    data, gap, status = websocket["schema"]["oneOf"]
    assert set(data["properties"]) == {"type", "fileId", "sessionId", "runId", "offset", "endOffset", "data", "size", "cursor"}
    assert set(gap["properties"]) == {"type", "message"}
    assert set(status["properties"]) == {"type", "status"}
    assert "4408" in websocket["conditional"][0]


def test_task_delete_permission_and_batch_idempotency_match_mutation_routes(client):
    """新写接口必须同步暴露实际授权范围和必需的幂等请求头。"""
    reference = client.get("/api/v1/api-reference").json()
    operations = {item["id"]: item for item in reference["operations"]}
    deletion = operations["DELETE /api/v1/tasks/{task_id}"]
    assert "tasks:write" in deletion["permission"] and "tasks:control" in deletion["permission"]
    assert "创建者" in deletion["permission"] and deletion["responseStatus"] == 202
    batch = operations["POST /api/v1/admin/common-commands/batch"]
    assert "Idempotency-Key" in batch["headers"]


def test_batch_resource_creation_documents_input_and_device_authentication_422_variants(client):
    """同一 422 状态码的输入校验与认证失败必须公开各自真实的 details 结构。"""
    reference = client.get("/api/v1/api-reference").json()
    operation = next(item for item in reference["operations"] if item["id"] == "POST /api/v1/resources/batch")
    error = next(item for item in operation["responseContract"]["errors"] if item["status"] == 422)
    variants = error["schema"]["oneOf"]
    business = next(item for item in variants if item["properties"]["error"]["properties"]["code"].get("const") == "BATCH_AUTHENTICATION_FAILED")
    failure = business["properties"]["error"]["properties"]["details"]["properties"]["items"]["items"]
    assert set(failure["properties"]) == {"index", "ip", "code"}
