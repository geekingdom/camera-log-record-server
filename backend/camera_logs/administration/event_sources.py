"""管理事件来源展示：只解释事件自身可证实的主体，不推断历史操作者。"""

from collections.abc import Iterable


async def _documents_by_id(db, collection: str, identifiers: set[str], projection: dict) -> dict[str, dict]:
    """按 ID 批量读取来源展示字段，避免把服务账号密文或节点配置送入管理响应。"""
    if not identifiers:
        return {}
    return {item["id"]: item async for item in db[collection].find(
        {"id": {"$in": list(identifiers)}}, projection=projection,
    ) if item.get("id")}


async def event_source_documents(db, items: Iterable[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    """预取服务账号和节点，使页面事件不会因逐条查询产生 N+1 数据库访问。"""
    raw_items = list(items)
    token_ids = {str(item["serviceTokenId"]) for item in raw_items if item.get("serviceTokenId")}
    node_ids = {str(item["nodeId"]) for item in raw_items if item.get("nodeId")}
    tokens = await _documents_by_id(db, "tokens", token_ids, {"id": 1, "name": 1})
    nodes = await _documents_by_id(db, "nodes", node_ids, {"id": 1, "name": 1})
    return tokens, nodes


def event_source(item: dict, *, users: dict[str, dict], tokens: dict[str, dict], nodes: dict[str, dict],
                 task: dict | None = None) -> dict[str, str]:
    """从不可变事件字段生成来源标签；任务只用于说明运行上下文，绝不反推创建者。"""
    actor = str(item["actor"]) if item.get("actor") else ""
    token_id = str(item["serviceTokenId"]) if item.get("serviceTokenId") else ""
    token = tokens.get(token_id)
    token_name = (token or {}).get("name") or token_id
    if actor == "system":
        return {"sourceKind": "SYSTEM", "sourceName": "平台后台", "sourceDetail": "后台自动处理（无客户端地址）"}
    if actor == "anonymous":
        client_ip = str(item["clientIp"]) if item.get("clientIp") else ""
        detail = {"sourceDetail": f"来源 IP：{client_ip}"} if client_ip else {}
        return {"sourceKind": "ANONYMOUS", "sourceName": "未认证身份", **detail}
    if actor == "bootstrap":
        return {"sourceKind": "BOOTSTRAP", "sourceName": "平台引导服务账号"}
    if actor:
        user = users.get(actor)
        source_name = (user or {}).get("displayName") or (user or {}).get("username") or actor
        details = [f"用户 ID：{actor}"]
        if token_id:
            details.append(f"服务账号：{token_name}")
        if client_ip := item.get("clientIp"):
            details.append(f"来源 IP：{client_ip}")
        return {"sourceKind": "USER", "sourceName": source_name, "sourceDetail": "；".join(details)}
    if token_id:
        return {"sourceKind": "SERVICE_TOKEN", "sourceName": token_name, "sourceDetail": f"服务账号 ID：{token_id}"}
    node_id = str(item["nodeId"]) if item.get("nodeId") else ""
    if node_id:
        node = nodes.get(node_id)
        return {"sourceKind": "NODE", "sourceName": f"采集节点：{(node or {}).get('name') or node_id}",
                "sourceDetail": f"节点 ID：{node_id}"}
    if item.get("type"):
        if item.get("taskId"):
            task_name = (task or {}).get("name") or str(item["taskId"])
            return {"sourceKind": "TASK_RUNTIME", "sourceName": "任务运行事件", "sourceDetail": f"任务：{task_name}"}
        return {"sourceKind": "SYSTEM_RUNTIME", "sourceName": "系统运行事件", "sourceDetail": "后台自动处理（无客户端地址）"}
    if item.get("route") or item.get("method"):
        client_ip = str(item["clientIp"]) if item.get("clientIp") else ""
        detail = {"sourceDetail": f"来源 IP：{client_ip}"} if client_ip else {}
        if item.get("httpStatus") == 401:
            return {"sourceKind": "UNAUTHENTICATED_HTTP", "sourceName": "认证未通过的请求", **detail}
        return {"sourceKind": "REQUEST_IDENTITY_MISSING", "sourceName": "请求来源（未记录身份）", **detail}
    return {"sourceKind": "HISTORICAL", "sourceName": "历史记录缺少身份"}
