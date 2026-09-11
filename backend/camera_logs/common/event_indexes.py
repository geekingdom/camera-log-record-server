"""安装增长型审计、运行和请求事件的查询索引。

管理事件页面允许按多个可选字段组合筛选。MongoDB 不能以一条索引同时覆盖
所有字段排列，因此每个可单独高频筛选的维度都以 ``字段 + createdAt`` 建立
小型复合索引。查询规划器选择最具选择性的前缀，剩余条件在有限候选集过滤；
不为任意组合建立指数级索引，控制每次写入的索引维护成本。
"""

from __future__ import annotations

from typing import Any

from pymongo.errors import OperationFailure

EVENT_INDEXES: dict[str, tuple[tuple[str, tuple[tuple[str, int], ...]], ...]] = {
    "audit": (
        ("audit_created_at_id", (("createdAt", -1), ("_id", -1))),
        ("audit_action_created_at_id", (("action", 1), ("createdAt", -1), ("_id", -1))),
        ("audit_actor_created_at_id", (("actor", 1), ("createdAt", -1), ("_id", -1))),
        ("audit_target_created_at_id", (("targetId", 1), ("createdAt", -1), ("_id", -1))),
        ("audit_request_created_at_id", (("requestId", 1), ("createdAt", -1), ("_id", -1))),
    ),
    "events": (
        ("events_created_at_id", (("createdAt", -1), ("_id", -1))),
        ("events_task_created_at_id", (("taskId", 1), ("createdAt", -1), ("_id", -1))),
        ("events_node_created_at_id", (("nodeId", 1), ("createdAt", -1), ("_id", -1))),
        ("events_type_created_at_id", (("type", 1), ("createdAt", -1), ("_id", -1))),
        ("events_request_created_at_id", (("requestId", 1), ("createdAt", -1), ("_id", -1))),
    ),
    "request_events": (
        ("request_events_created_at_id", (("createdAt", -1), ("_id", -1))),
        ("request_events_task_created_at_id", (("taskId", 1), ("createdAt", -1), ("_id", -1))),
        ("request_events_status_created_at_id", (("httpStatus", 1), ("createdAt", -1), ("_id", -1))),
        ("request_events_route_created_at_id", (("route", 1), ("createdAt", -1), ("_id", -1))),
        ("request_events_client_ip_created_at_id", (("clientIp", 1), ("createdAt", -1), ("_id", -1))),
        ("request_events_request_created_at_id", (("requestId", 1), ("createdAt", -1), ("_id", -1))),
    ),
}


async def _create_or_reuse_index(collection: Any, keys: tuple[tuple[str, int], ...], *, name: str,
                                  **options: Any) -> str:
    """按键和关键选项复用历史匿名索引，避免迁移时因改名触发 code 85。"""
    indexes = await collection.index_information()
    matching = [(existing_name, definition) for existing_name, definition in indexes.items()
                if tuple(definition.get("key", ())) == keys]
    for existing_name, definition in matching:
        if (definition.get("partialFilterExpression") is not None or definition.get("sparse", False)
                or definition.get("hidden", False)):
            continue
        # TTL 是唯一必须严格一致的索引选项；同键的普通索引直接复用即可。
        requested_ttl = options.get("expireAfterSeconds")
        existing_ttl = definition.get("expireAfterSeconds")
        if requested_ttl is not None and existing_ttl != requested_ttl:
            raise RuntimeError(f"索引 {existing_name} 的 TTL 与策略不一致")
        return existing_name
    if matching:
        names = ", ".join(name for name, _ in matching)
        raise RuntimeError(f"已有同键索引 {names} 含 partial、sparse 或 hidden 选项，需先迁移")
    try:
        return await collection.create_index(keys, name=name, **options)
    except OperationFailure as error:
        # 多个 API/Worker 并发初始化可能刚好由其他进程创建同键索引；
        # 重新读取确认键和 TTL 后再复用，避免吞掉真正的索引冲突。
        if error.code != 85:
            raise
        indexes = await collection.index_information()
        for existing_name, definition in indexes.items():
            if (tuple(definition.get("key", ())) == keys and definition.get("partialFilterExpression") is None
                    and not definition.get("sparse", False) and not definition.get("hidden", False)):
                if options.get("expireAfterSeconds") not in (None, definition.get("expireAfterSeconds")):
                    raise RuntimeError(f"并发创建的索引 {existing_name} 的 TTL 与策略不一致")
                return existing_name
        raise


async def install_event_indexes(db: Any) -> None:
    """幂等创建管理查询索引及请求事件 TTL 索引。

    索引键严格匹配正式查询的 ``createdAt DESC, _id DESC`` 稳定排序，避免同一
    时间戳下增加阻塞排序。``createdAt`` 同时作为所有正式管理读取路径的时间
    范围字段，前缀字段则对应管理员可独立使用的筛选条件。
    """
    for collection, definitions in EVENT_INDEXES.items():
        for name, keys in definitions:
            await _create_or_reuse_index(db[collection], keys, name=name)
    # 访问排障记录与业务审计生命周期不同：只保留 30 天，避免每条 HTTP
    # 请求持续累积。TTL 清理是异步的，不应用于任务恢复所依赖的集合。
    await _create_or_reuse_index(
        db.request_events, (("createdAt", 1),),
        expireAfterSeconds=30 * 24 * 60 * 60, name="request_events_ttl_created_at"
    )
