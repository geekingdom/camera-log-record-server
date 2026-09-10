"""从实际OpenAPI路径构造完整目录，补齐权限、中文分组及第三方调用说明。"""

from camera_logs.reference.examples import request_example, response_example, schema_example

GUIDES = [
    {"title": "设备重启与暂停恢复", "text": "SSH暂停释放连接并保留运行预算。设备重启期间，周期认证可以显示离线，但不会把用户暂停改成停止。显式resume返回202和操作ID，任务进入WAITING_DEVICE并等待新认证；即使缓存显示ONLINE也会重新探测。等待时可暂停或停止取消；同一设备恢复沿用预算，身份变更后建立新运行。操作到COLLECTING才完成。因认证失败被系统停止的其他任务，需用户编辑并成功认证后才恢复，手动停止任务不自动启动。"},
    {"title": "Coredump接收与下载", "text": "海康网络资源的SSH任务可设置enableCoredumpMonitor=true；节点部署需配置NFS_ROOT和NFS_SERVER_IP，NFS允许所有网络可达来源，不使用平台IP白名单筛选设备。GET /api/v1/resources/{resource_id}/coredumps按name字面文件名及receivedFrom/receivedTo接收时间查询。RECEIVING代表已观测文件，FROZEN代表服务器固定副本，并非设备完成崩溃文件的证明。POST /api/v1/coredump-exports提交fileIds并携带Idempotency-Key，轮询返回ID的状态；单文件导出原文件，多文件ZIP STORE。成功后GET /api/v1/coredump-exports/{identifier}/content支持Range和If-Range，适合流式或断点下载；不得把大文件整体装入客户端内存。"},
    {"title": "查看服务账号口令", "text": "管理员可查看全部服务账号，普通用户只可查看绑定给本人的账号。GET /api/v1/service-tokens和POST /api/v1/service-tokens/{id}/reveal要求service-tokens:read；列表不含口令，reveal返回token并记录无敏感内容的审计。新口令加密保存可重复查看，旧版仅保存摘要的口令无法还原。管理员可用POST /api/v1/service-tokens/{id}/rotate携带version重新生成，旧口令立即失效；普通用户不能新增、编辑、撤销或重新生成。永久有效不绕过用户禁用、删除和来源IP限制。"},
    {"title": "第三方鉴权", "text": "管理员通过服务令牌接口创建可撤销Token。普通HTTP请求携带Authorization: Bearer <SERVICE_TOKEN>。Token范围与平台来源IP权限取交集；IP白名单只约束调用方，不约束设备IP。接口目录不授予接口执行权限。"},
    {"title": "资源发现与任务关联", "text": "GET /api/v1/resources 的search匹配资源名、IP、型号、序列号和软件版本。name、model、subSerialNumber、softwareVersion是字面子串；ip精确匹配；同时传入的条件取交集。每项包含tasks摘要，默认100条、taskLimit最大500；tasksTruncated=true时通过tasksUrl继续分页。拥有tasks:read的有效用户可读取全部资源和任务，不再按资源或任务ID白名单收窄可见范围。"},
    {"title": "按任务ID控制", "text": "POST /api/v1/tasks/{task_id}/start、stop、pause、resume返回202及operation id；轮询GET /api/v1/operations/{identifier}确认完成。pause/resume仅SSH，暂停释放连接并保留运行预算；停止后启动建立新运行。202不代表设备已经连接。"},
    {"title": "幂等、版本与错误", "text": "创建资源、任务、模板、下载、检索和发送命令必须传Idempotency-Key，长度1至128。同键同请求重放，不同请求冲突409。编辑及节点删除需当前version。错误统一包含error.code/message/requestId；422还包含details。401为认证失效，403为权限不足，503为暂不可确认。"},
    {"title": "按时间查询日志", "text": "POST /api/v1/log-searches提交taskId、start、end，可省略keyword。时间为带时区ISO8601，最多24小时，省略时默认最近一小时。空keyword按采集块接收时间的[start,end)返回字节片段，每片不超过4096字节，data为Base64，按fileId与offset无损拼接；text仅供预览。无完整时间索引则失败，不能猜测日志时间。非空keyword为字面检索。"},
    {"title": "查询结果与完整日志", "text": "检索返回作业id；查询作业状态后，通过/log-searches/{id}/results分页获取结果，最多1000项，truncated=true表示需缩短区间或读取原文件。按文件的/content?offset=0&limit=65536可续读，limit最大262144。搜索结果上限不影响原始日志保存。"},
    {"title": "小时下载与断点续传", "text": "先用/tasks/{task_id}/log-hours取得hourId，可传date按北京时间筛选日期，再POST /downloads提交taskId和hourIds。单次最多168小时、预计20GB。轮询成功后GET /downloads/{id}/content，携带Bearer及可选Range。单小时下载仅含日志的tar.gz，多小时ZIP STORE。缺片默认失败；allowPartial=true才允许部分导出。"},
    {"title": "实时日志", "text": "HTTP部署使用ws://<host>/api/v1/tasks/{task_id}/logs；HTTPS部署使用wss://<host>/api/v1/tasks/{task_id}/logs。连接后10秒内发送JSON首帧token及可选cursor，不能把Token放URL。服务端每次推送重验权限；data为Base64原始字节，fileId/offset用于消除重叠后再解码。gap表示实时缓冲缺口，按文件或小时归档补读。重连提交最后cursor，不保证断线期间设备输出可追回。"},
    {"title": "手动命令", "text": "POST /tasks/{task_id}/commands提交command及可选newline/delaySeconds/prompt/timeoutSeconds，使用同一采集连接。初始化未完成或断线拒绝；通过/commands/{id}或/task的command-executions查询。最多发送次数不代表设备业务成功，UNKNOWN不自动重发。"},
    {"title": "用户权限与所有权", "text": "有效普通用户自动拥有tasks:read、logs:read、logs:download、templates:read、templates:write和service-tokens:read；用户配置的scopes仅增加写入、控制、命令或管理能力。平台来源IP策略仍会收窄本次请求的有效权限。服务令牌实时继承绑定用户当前权限、管理员身份和启用状态。所有人可读取资源、任务、下载和只读日志；资源和任务的编辑、删除或控制，以及向任务发送手动命令，均须具备对应scope且仅创建者或管理员可执行。创建任务可使用他人创建的资源，任务自身独立归属。"},
    {"title": "模板共享", "text": "服务端写入模板创建者，客户端不能提交createdBy。创建者、sharedWith中的有效用户及sharedWithAll=true时的全部有效用户可读取模板；管理员可读取全部模板。仅创建者或管理员可编辑、删除模板；sharedWith中的用户必须存在、启用且未删除，sharedWithAll仅管理员可设置。模板名称按创建者唯一。任务只在创建或实际切换模板来源时校验模板可读，已保存的任务快照不会因撤销共享或删除模板失效。GET /api/v1/users/share-targets要求templates:read，返回可共享对象的最小用户列表。"},
    {"title": "示例与运行环境", "text": "下列内容均为虚拟示例，标识需替换为实际返回值。成功响应展示代表字段，可能包含更多运行信息。文档与代码一同部署，不请求GitHub或外部CDN。这里不自动执行接口；实际调用会按权限记录审计。"},
]

TITLES = {"get": "查询", "post": "创建", "patch": "编辑", "delete": "删除"}
SPECIAL = {"start": "启动任务", "stop": "停止任务", "pause": "暂停SSH任务", "resume": "恢复SSH任务",
           "creators": "查询历史创建用户",
           "reveal": "查看服务账号口令", "rotate": "重新生成服务账号口令",
           "authenticate": "认证设备资源", "login": "账号登录", "logout": "退出登录", "password": "修改本人密码",
           "me": "查询当前登录用户", "reset-password": "重置子账户密码", "confirm-isolation": "确认旧节点已隔离",
           "browser-session": "签发浏览器下载授权", "results": "分页查询检索结果", "log-hours": "查询任务小时日志",
           "command-executions": "查询任务命令记录", "permissions": "查询可配置权限", "share-targets": "查询模板共享对象", "content": "读取日志内容"}


def group(path):
    """按正式路径归类，不依赖前端功能是否对当前用户展示。"""
    for fragment, title in (("coredump", "Coredump文件"), ("resources", "设备资源"), ("command-templates", "命令模板"),
                             ("commands", "命令交互"), ("command-executions", "命令交互"),
                             ("log-", "日志查询"), ("downloads", "日志下载"), ("tasks", "采集任务"),
                             ("operations", "异步操作"), ("service-tokens", "服务令牌"),
                             ("users", "用户管理"), ("auth/", "登录会话"), ("events", "审计事件"),
                             ("nodes", "节点管理"), ("ip-policy", "来源IP策略"), ("platform-settings", "平台配置")):
        if fragment in path:
            return title
    return "平台接口"


def permission(path, method):
    """标明实际scope、所有权和会话例外，避免目录沿用已移除的ID白名单。"""
    if path == "/health":
        return "无需Token"
    if "/auth/" in path:
        if path.endswith("/login"):
            return "无需预先认证（仍受平台来源IP策略约束）"
        if path.endswith("/logout"):
            return "无需预先认证；携带登录会话Cookie时撤销该会话"
        return "仅Cookie登录会话"
    if path == "/api/v1/users/share-targets":
        return "templates:read"
    if path == "/api/v1/users/creators":
        return "tasks:read 或 templates:read（最小历史创建人目录）"
    if "/service-tokens" in path and (method == "GET" or path.endswith("/reveal")):
        return "service-tokens:read + 仅绑定用户本人或admin"
    if any(part in path for part in ("/users", "/service-tokens", "/admin/", "/nodes", "-events", "/platform-settings")) or path == "/metrics":
        return "admin"
    if "api-reference" in path:
        return "有效Token或登录会话"
    if "coredump" in path:
        if path.endswith("/coredumps"):
            return "logs:read"
        return "logs:download" + (" + 导出创建者或admin" if "/coredump-exports/" in path else "")
    if "/resources" in path:
        if method == "GET":
            return "tasks:read"
        if method == "POST":
            return "resources:create"
        return "resources:write + 仅创建者或admin" + (" + tasks:control" if method == "DELETE" else "")
    if "/command-templates" in path:
        if method == "GET":
            return "templates:read"
        return "templates:write" if method == "POST" else "templates:write + 仅创建者或admin"
    if "/downloads" in path:
        return "logs:read（取消本人作业；他人作业另需admin）" if method == "DELETE" else "logs:download"
    if "/log-" in path or path.endswith("/logs"):
        return "logs:read"
    if path.endswith("/commands") and method == "POST":
        return "commands:send + 仅任务创建者或admin"
    if any(path.endswith("/" + action) for action in ("start", "stop", "pause", "resume")):
        return "tasks:control + 仅任务创建者或admin"
    if path.endswith("/tasks") and method == "POST":
        return "tasks:create；autoStart=true另需tasks:control"
    return "tasks:write + 仅任务创建者或admin" if method == "PATCH" else "tasks:read"


def request_headers(path, method, schema, idempotent):
    """按实际鉴权入口生成调用头，避免把登录和浏览器下载误写成 Bearer 专用。"""
    if path == "/health":
        headers = {}
    elif path.endswith("/auth/login"):
        headers = {"X-Requested-With": "XMLHttpRequest"}
    elif path.endswith("/auth/logout"):
        headers = {"X-Requested-With": "XMLHttpRequest", "Cookie": "camera_session=<登录会话Cookie>"}
    elif path.endswith("/auth/me"):
        headers = {"Cookie": "camera_session=<登录会话Cookie>"}
    elif path.endswith("/auth/password"):
        headers = {"X-Requested-With": "XMLHttpRequest", "Cookie": "camera_session=<登录会话Cookie>"}
    elif path.endswith("/content") and any(fragment in path for fragment in ("/downloads/", "/coredumps/", "/coredump-exports/")):
        headers = {"Authorization": "Bearer <SERVICE_TOKEN>（或 camera_session / download_access Cookie）"}
    elif "api-reference" in path:
        headers = {"Authorization": "Bearer <SERVICE_TOKEN>（或已登录会话 Cookie）"}
    else:
        headers = {"Authorization": "Bearer <SERVICE_TOKEN>"}
    if idempotent:
        headers["Idempotency-Key"] = "example-unique-request-key"
    if schema:
        headers["Content-Type"] = "application/json"
    return headers


def catalog(app):
    """完整枚举公共HTTP接口，再补充OpenAPI没有表达的WebSocket握手与消息。"""
    spec = app.openapi()
    schemas = spec.get("components", {}).get("schemas", {})
    operations = []
    for path, methods in spec["paths"].items():
        for method, definition in methods.items():
            if method not in {"get", "post", "patch", "delete", "put", "head", "options"}:
                continue
            verb = method.upper()
            schema = definition.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
            code = min(int(code) for code in definition["responses"] if code.startswith("2"))
            title = SPECIAL.get(path.split("/")[-1], TITLES.get(method, verb) + group(path))
            idempotent = method == "post" and (path.split("/")[-1] in {"resources", "tasks", "command-templates", "downloads", "log-searches", "commands", "coredump-exports"})
            headers = request_headers(path, verb, schema, idempotent)
            parameters = [{**item, "example": schema_example(item.get("schema", {}), schemas, item["name"])}
                          for item in definition.get("parameters", [])]
            operations.append({"id": f"{verb} {path}", "method": verb, "path": path, "title": title,
                               "group": group(path), "description": definition.get("description", "按当前权限执行操作，响应字段随状态变化。"),
                               "permission": permission(path, verb), "headers": headers, "parameters": parameters,
                               "requestSchema": schema, "requestExample": request_example(schema, schemas) if schema else None,
                               "responseStatus": code, "responseExample": response_example(path, verb, code)})
    operations.append({"id": "WS /api/v1/tasks/{task_id}/logs", "method": "WS", "path": "/api/v1/tasks/{task_id}/logs",
                       "title": "订阅任务实时日志", "group": "日志查询", "description": next(item["text"] for item in GUIDES if item["title"] == "实时日志"),
                       "permission": "logs:read", "headers": {}, "parameters": [{"name": "task_id", "in": "path", "required": True, "schema": {"type": "string"}, "example": "task-example"}],
                       "requestSchema": None, "requestExample": {"token": "<SERVICE_TOKEN>", "cursor": ""},
                       "responseStatus": 101, "responseExample": {"type": "data", "fileId": "file-example", "offset": 0, "data": "bG9nCg==", "cursor": "<续传游标>"}})
    return {"version": "1.0", "guides": GUIDES, "operations": operations, "schemas": schemas,
            "errorExample": {"error": {"code": "403", "message": "任务不在授权范围内", "requestId": "request-example"}}}
