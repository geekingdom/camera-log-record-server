"""为审计、运行和请求事件批量补齐中文展示字段并脱敏原因。"""

from collections.abc import Iterable

from camera_logs.common.database import public
from camera_logs.common.observability import redact, redact_text

ACTION_SUMMARIES = {
    "control:RUNNING": "请求启动任务", "control:STOPPED": "请求停止任务",
    "control:PAUSED": "请求暂停任务", "create_task": "创建任务",
    "edit_task": "修改任务", "delete_task": "删除任务",
    "create_resource": "创建设备资源", "edit_resource": "修改设备资源",
    "delete_resource": "删除设备资源", "create_template": "创建命令模板",
    "edit_template": "修改命令模板", "delete_template": "删除命令模板",
    "create_token": "创建服务令牌", "revoke_token": "撤销服务令牌",
    "update_platform_settings": "修改平台设置", "update_ip_policy": "修改来源访问规则",
    "confirm_node_isolation": "确认节点外部隔离",
    "login": "用户登录", "login_failed": "用户登录失败", "logout": "用户退出登录",
    "change_password": "修改账户密码", "create_user": "创建用户", "edit_user": "修改用户",
    "delete_user": "删除用户", "register_node": "登记节点", "edit_node": "修改节点配置",
    "browser_download_authorization": "授权浏览器下载", "download_content": "下载日志内容",
    "live_subscribe": "订阅实时日志", "job_succeeded": "日志作业完成", "job_failed": "日志作业失败",
}
EVENT_SUMMARIES = {
    "CONNECTION_GAP": "采集连接中断", "USER_PAUSED": "任务已暂停",
    "DISK_PRESSURE_CHANGED": "节点磁盘压力变化", "WRITE_PRESSURE_CHANGED": "节点写入压力变化",
    "EXTERNAL_FENCING_CONFIRMED": "确认节点外部隔离", "DEBUG_MODE": "调试模式状态变化",
    "CLOCK_ROLLBACK": "服务器时间回拨",
}
_PRESSURE_EVENTS = {"DISK_PRESSURE_CHANGED", "WRITE_PRESSURE_CHANGED"}
_TERMINAL_OUTCOMES = {"PENDING", "SUCCEEDED", "FAILED", "CANCELLED", "UNKNOWN"}
_TARGET_COLLECTIONS = {
    "tasks": {"id": 1, "name": 1, "ip": 1}, "resources": {"id": 1, "name": 1, "ip": 1},
    "templates": {"id": 1, "name": 1}, "users": {"id": 1, "username": 1, "displayName": 1},
    "nodes": {"id": 1, "name": 1}, "node_configs": {"id": 1, "name": 1},
    "jobs": {"id": 1, "name": 1}, "commands": {"id": 1, "taskId": 1, "kind": 1},
}


def event_outcome(item: dict) -> str:
    """按事件本身的语义推导结果，兼容未持久化展示字段的历史记录。"""
    if item.get("outcome") in _TERMINAL_OUTCOMES:
        return item["outcome"]
    if item.get("responseComplete") is False:
        return "UNKNOWN"
    if item.get("status") in _TERMINAL_OUTCOMES:
        return item["status"]
    if isinstance(item.get("httpStatus"), int):
        if item["httpStatus"] == 202:
            return "PENDING"
        return "FAILED" if item["httpStatus"] >= 400 else "SUCCEEDED"
    event_type = item.get("type")
    if event_type in {"CONNECTION_GAP", "CLOCK_ROLLBACK"}:
        return "UNKNOWN"
    if event_type in _PRESSURE_EVENTS:
        return "SUCCEEDED" if item.get("level") == "NORMAL" else "UNKNOWN"
    if event_type == "DEBUG_MODE":
        return "FAILED" if item.get("debugError") else "SUCCEEDED"
    return "SUCCEEDED"


def event_level(item: dict, outcome: str | None = None) -> str:
    """将业务压力等级转换为展示告警级别，避免把 NORMAL 当作日志级别返回。"""
    outcome = outcome or event_outcome(item)
    event_type = item.get("type")
    if event_type in _PRESSURE_EVENTS:
        pressure = item.get("level")
        return "ERROR" if pressure == "CRITICAL" else "WARNING" if pressure != "NORMAL" else "INFO"
    if item.get("level") in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        return item["level"]
    if outcome == "FAILED":
        return "ERROR"
    if outcome in {"UNKNOWN", "CANCELLED"}:
        return "WARNING"
    return "INFO"


def _summary(item: dict) -> str:
    """提供无需读取原始事件内容的中文短摘要。"""
    if item.get("summary"):
        return redact_text(str(item["summary"]))
    if item.get("action"):
        return ACTION_SUMMARIES.get(item["action"], "执行管理操作")
    if item.get("type"):
        return EVENT_SUMMARIES.get(item["type"], "记录运行事件")
    if item.get("route"):
        return f"HTTP 请求：{item.get('method', 'REQUEST')} {item['route']}"
    return "记录平台事件"


def _reason(item: dict) -> str | None:
    """从可诊断字段提取单一安全原因，优先处理明确失败和异常字段。"""
    for field in ("reason", "error", "debugError", "message"):
        value = item.get(field)
        if value:
            return redact_text(str(redact(value)))
    return None


async def _documents_by_id(db, collection: str, identifiers: set[str]) -> dict[str, dict]:
    """仅投影展示所需字段批量读取关联对象，禁止把凭据或散列带入事件页面。"""
    if not identifiers:
        return {}
    return {item["id"]: item async for item in db[collection].find(
        {"id": {"$in": list(identifiers)}}, projection=_TARGET_COLLECTIONS[collection]) if item.get("id")}


async def present_events(db, items: Iterable[dict]) -> list[dict]:
    """批量解析主体、任务和目标名称，避免分页条目逐行查询造成 N+1。"""
    raw_items = list(items)
    actor_ids = {str(item["actor"]) for item in raw_items if item.get("actor")}
    target_ids = {str(item["targetId"]) for item in raw_items if item.get("targetId")}
    task_ids = {str(item["taskId"]) for item in raw_items if item.get("taskId")} | target_ids
    users = await _documents_by_id(db, "users", actor_ids | target_ids)
    documents = {name: await _documents_by_id(db, name, task_ids if name == "tasks" else target_ids)
                 for name in _TARGET_COLLECTIONS if name != "users"}
    documents["users"] = users
    linked_task_ids = task_ids | {str(item["taskId"]) for name in ("jobs", "commands")
                                  for item in documents[name].values() if item.get("taskId")}
    if linked_task_ids != task_ids:
        documents["tasks"] = await _documents_by_id(db, "tasks", linked_task_ids)
    result = []
    for raw in raw_items:
        item = public(redact(raw))
        for forbidden in ("authorization", "headers", "body", "response", "responseBody", "requestBody"):
            item.pop(forbidden, None)
        outcome = event_outcome(item)
        item.update(summary=_summary(item), outcome=outcome, level=event_level(item, outcome))
        reason = _reason(item)
        if reason:
            item["reason"] = reason
        else:
            item.pop("reason", None)
        for diagnostic in ("error", "message", "debugError"):
            item.pop(diagnostic, None)
        if user := users.get(str(item.get("actor"))):
            item["actorName"] = user.get("displayName") or user.get("username") or user["id"]
        task = documents["tasks"].get(str(item.get("taskId") or item.get("targetId")))
        if task:
            item.setdefault("taskId", task["id"])
            item["taskName"] = task.get("name") or task["id"]
            item["deviceIp"] = task.get("ip")
        target_id = str(item.get("targetId") or "")
        if target_id:
            target = next((group[target_id] for group in documents.values() if target_id in group), None)
            target_name = target.get("name") if target else None
            if target and target_id in users:
                target_name = target.get("displayName") or target.get("username") or target_id
            item["targetName"] = item.get("targetName") or target_name or target_id
            if target and target.get("taskId") and not item.get("taskName"):
                linked_task = documents["tasks"].get(str(target["taskId"]))
                if linked_task:
                    item["taskId"] = linked_task["id"]
                    item["taskName"] = linked_task.get("name") or linked_task["id"]
                    item["deviceIp"] = linked_task.get("ip")
        result.append(item)
    return result
