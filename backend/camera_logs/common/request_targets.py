"""从已匹配的 API 路由提取请求对象，集合操作不伪造单个实体标识。"""

TARGET_LABELS = {
    "nodes": "服务节点", "resources": "设备资源", "tasks": "采集任务",
    "command-templates": "命令模板", "commands": "命令执行", "users": "用户账户",
    "service-tokens": "第三方服务账号", "downloads": "日志下载作业", "log-searches": "日志检索作业",
    "log-files": "日志文件", "coredumps": "Coredump文件", "coredump-exports": "Coredump导出作业",
    "operations": "任务操作", "platform-settings": "后台配置", "display-settings": "实时日志显示配置", "ip-policy": "平台IP访问规则",
    "auth": "平台登录会话", "api-reference": "开放API文档", "audit-events": "审计记录",
    "runtime-events": "运行事件", "request-events": "请求记录",
}
PLATFORM_TARGETS = {"platform-settings", "display-settings", "ip-policy", "auth", "api-reference"}
IDENTIFIERS = {"identifier", "task_id", "resource_id", "node_id"}


def request_target(route: str, path_params: dict) -> dict:
    """只提取路由声明的对象 ID；不扫描请求正文、查询、任意路径或认证字段。"""
    parts = route.strip("/").split("/")
    if parts[:2] != ["api", "v1"]:
        return {}
    parts = parts[2:]
    if parts and parts[0] == "admin":
        parts = parts[1:]
    kind = parts[0] if parts else ""
    if kind not in TARGET_LABELS:
        return {"targetLabel": "平台接口", "targetScope": "PLATFORM"}
    result = {"targetKind": kind, "targetLabel": TARGET_LABELS[kind],
              "targetScope": "PLATFORM" if kind in PLATFORM_TARGETS else "COLLECTION"}
    if len(parts) > 1 and parts[1].startswith("{") and parts[1].endswith("}"):
        key = parts[1][1:-1]
        value = path_params.get(key) if key in IDENTIFIERS else None
        result["targetScope"] = "OBJECT"
        if isinstance(value, str) and value:
            result["targetId"] = value[:128]
            field = {"tasks": "taskId", "resources": "resourceId", "nodes": "nodeId"}.get(kind)
            if field:
                result[field] = result["targetId"]
    return result
