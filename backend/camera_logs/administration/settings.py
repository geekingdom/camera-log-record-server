"""提供管理员可修改的平台保留期和节点准入配置。

节点进程写入的心跳保存在 ``nodes``，管理配置单独保存在 ``node_configs``，
避免心跳覆盖人工设置，也避免登记请求伪造在线节点。
"""

from datetime import timedelta
from ipaddress import ip_address
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from camera_logs.administration.node_lifecycle import delete_node
from camera_logs.common.audited_mutations import audited_mutation
from camera_logs.common.config import (
    DEFAULT_CLUSTER_CAPACITY,
    MAX_CLUSTER_CAPACITY,
    MAX_NODE_CAPACITY,
)
from camera_logs.common.database import now
from camera_logs.common.security import actor, authorize

PLATFORM_SETTINGS_ID = "platform"
DEFAULT_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 3650
ONLINE_HEARTBEAT_AGE = timedelta(seconds=30)


class SettingsModel(BaseModel):
    """管理员配置请求模型拒绝额外字段，防止未知参数被静默保存。"""

    model_config = ConfigDict(extra="forbid")


class PlatformSettingsPatch(SettingsModel):
    """平台保留期更新携带乐观锁版本，避免后写覆盖其他管理员修改。"""

    retentionDays: int = Field(ge=1, le=MAX_RETENTION_DAYS, strict=True)
    clusterCapacity: int | None = Field(default=None, ge=1, le=MAX_CLUSTER_CAPACITY, strict=True)
    version: int = Field(ge=1, strict=True)


class NodeRegistration(SettingsModel):
    """定义管理员托管的节点配置，未登记 worker 保留原有部署兼容行为。"""

    id: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=1, max_length=2048)
    capacity: int = Field(ge=1, le=MAX_NODE_CAPACITY, strict=True)
    accepting: bool = True

    @field_validator("id")
    @classmethod
    def normalize_id(cls, value: str) -> str:
        """去除节点标识空白，保证后续 worker 使用的是同一个稳定 ID。"""
        if not value.strip():
            raise ValueError("节点 ID 不能为空")
        return value.strip()

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        """支持容器服务名和内网 HTTP(S) 公布地址，不把客户端白名单用于节点目标。"""
        if any(character.isspace() or ord(character) < 32 for character in value):
            raise ValueError("节点地址不能包含空白或控制字符")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("节点地址必须是 HTTP 或 HTTPS 地址")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("节点地址端口必须在 1 到 65535 之间") from exc
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("节点地址端口必须在 1 到 65535 之间")
        if any(character.isspace() for character in parsed.hostname):
            raise ValueError("节点地址主机名不能包含空白字符")
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("节点地址不得包含用户信息、查询参数、片段或路径")
        try:
            address = ip_address(parsed.hostname)
        except ValueError:
            address = None  # Docker 服务名与 DNS 名由实际部署网络解析。
        if address is not None and address.is_unspecified:
            raise ValueError("节点地址不能使用 0.0.0.0 或 :: 通配监听地址")
        return value.rstrip("/")


class NodeConfigPatch(SettingsModel):
    """节点配置只允许调整准入开关和容量，地址需通过重新登记审核。"""

    version: int = Field(ge=1, strict=True)
    capacity: int | None = Field(default=None, ge=1, le=MAX_NODE_CAPACITY, strict=True)
    accepting: bool | None = None


def _reported_at(node: dict | None) -> object | None:
    """把 worker 心跳转换为可序列化的真实报告时间，缺失时明确离线。"""
    return node.get("heartbeat") if node else None


def _is_online(node: dict | None) -> bool:
    """在线状态只根据 worker 心跳计算，登记配置不能使节点显示为在线。"""
    heartbeat = _reported_at(node)
    if heartbeat is None:
        return False
    return now() - heartbeat.replace(tzinfo=now().tzinfo) < ONLINE_HEARTBEAT_AGE


def _public_node_config(config: dict, node: dict | None) -> dict:
    """合并管理员配置与只读心跳事实，不采用 worker 可覆盖字段作为配置值。"""
    result = {
        "id": config["id"],
        "url": config["url"],
        "capacity": config["capacity"],
        "accepting": config["accepting"],
        "version": config["version"],
        "registered": True,
        "online": _is_online(node),
        "reportedAt": _reported_at(node),
    }
    if node:
        result["reportedUrl"] = node.get("url")
        result["urlMismatch"] = node.get("url") != config["url"]
        result["activeTasks"] = node.get("activeTasks", 0)
        result["diskPercent"] = node.get("diskPercent")
    return result


def _public_discovered_node(node: dict) -> dict:
    """公开只由 worker 上报的发现项，不生成管理配置或推断人工准入值。"""
    return {
        "id": node["id"],
        "url": node.get("url", ""),
        "reportedUrl": node.get("url"),
        "capacity": node.get("capacity", 0),
        "accepting": node.get("accepting", False),
        "version": 0,
        "registered": False,
        "online": _is_online(node),
        "reportedAt": _reported_at(node),
        "activeTasks": node.get("activeTasks", 0),
        "diskPercent": node.get("diskPercent"),
    }


async def _platform_settings(repo, *, session=None) -> dict:
    """读取或首次初始化系统默认配置；用户 PATCH 传入事务会话。"""
    # GET 可惰性建立系统默认值；用户变更必须传入 session，使初始化、更新和审计同提交。
    timestamp = now()
    configured_default = int(getattr(repo.settings, "retention_days", DEFAULT_RETENTION_DAYS))
    retention_days = configured_default if 1 <= configured_default <= MAX_RETENTION_DAYS else DEFAULT_RETENTION_DAYS
    configured_cluster = int(getattr(repo.settings, "cluster_capacity", 500))
    cluster_capacity = (configured_cluster if 1 <= configured_cluster <= MAX_CLUSTER_CAPACITY
                        else DEFAULT_CLUSTER_CAPACITY)
    return await repo.db.platform_settings.find_one_and_update(
        {"id": PLATFORM_SETTINGS_ID},
        {"$setOnInsert": {"id": PLATFORM_SETTINGS_ID, "retentionDays": retention_days,
                           "clusterCapacity": cluster_capacity,
                           "version": 1, "createdAt": timestamp, "updatedAt": timestamp}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
        session=session,
    )


def install_settings_routes(app):
    """安装平台设置和节点配置路由，全部要求管理员作用域。"""
    User = Annotated[dict, Depends(actor)]

    @app.get("/api/v1/platform-settings")
    async def platform_settings(request: Request, user: User):
        """返回版本化平台设置；首次读取建立可审计的默认配置记录。"""
        authorize(user, "admin")
        document = await _platform_settings(request.app.state.repo)
        result = {key: document.get(key) for key in ("retentionDays", "clusterCapacity", "version", "updatedAt")}
        result["clusterCapacity"] = result["clusterCapacity"] or request.app.state.repo.settings.cluster_capacity
        return result

    @app.patch("/api/v1/platform-settings")
    async def update_platform_settings(body: PlatformSettingsPatch, request: Request, user: User):
        """原子更新保留期，版本不一致时拒绝覆盖并提示客户端刷新。"""
        authorize(user, "admin")
        repo = request.app.state.repo

        async def commit(session):
            """默认记录、版本更新和审计必须在同一可重试事务中提交。"""
            await _platform_settings(repo, session=session)
            document = await repo.db.platform_settings.find_one_and_update(
                {"id": PLATFORM_SETTINGS_ID, "version": body.version},
                {"$set": {"retentionDays": body.retentionDays, "updatedAt": now(),
                           **({"clusterCapacity": body.clusterCapacity} if body.clusterCapacity is not None else {})},
                 "$inc": {"version": 1}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if not document:
                raise HTTPException(409, "平台配置已被其他管理员修改，请刷新后重试")
            return document

        document = await audited_mutation(
            repo, user["id"], "update_platform_settings", PLATFORM_SETTINGS_ID, commit
        )
        result = {key: document.get(key) for key in ("retentionDays", "clusterCapacity", "version", "updatedAt")}
        result["clusterCapacity"] = result["clusterCapacity"] or repo.settings.cluster_capacity
        return result

    @app.get("/api/v1/admin/nodes")
    async def node_configs(request: Request, user: User):
        """合并登记配置和实际心跳；发现项必须由管理员显式登记才成为配置。"""
        authorize(user, "admin")
        repo = request.app.state.repo
        configs = [item async for item in repo.db.node_configs.find({}).sort("id", 1)]
        heartbeats = {item["id"]: item async for item in repo.db.nodes.find({})}
        configured = {item["id"]: item for item in configs}
        deleted = {item["id"] for item in configs if item.get("deletedAt")}
        items = [
            _public_node_config(configured[identifier], heartbeats.get(identifier))
            if identifier in configured else _public_discovered_node(heartbeats[identifier])
            for identifier in sorted((configured.keys() | heartbeats.keys()) - deleted)
        ]
        return {"items": items}

    @app.post("/api/v1/admin/nodes", status_code=201)
    async def register_node(body: NodeRegistration, request: Request, user: User):
        """登记允许部署的节点，不写 nodes 集合，不能以管理请求伪造 worker 心跳。"""
        authorize(user, "admin")
        repo = request.app.state.repo
        timestamp = now()
        document = body.model_dump() | {"version": 1, "createdAt": timestamp, "updatedAt": timestamp}

        async def commit(session):
            """节点配置插入和登记审计共享事务会话，唯一索引冲突由路由保持原语义。"""
            previous = await repo.db.node_configs.find_one({"id": body.id}, session=session)
            if previous and previous.get("deletedAt"):
                document["version"] = previous["version"] + 1
                await repo.db.node_configs.replace_one({"id": body.id}, document, session=session)
                await repo.db.nodes.update_one({"id": body.id}, {"$unset": {"deletedAt": ""}}, session=session)
            else:
                await repo.db.node_configs.insert_one(document, session=session)
            return document

        try:
            await audited_mutation(repo, user["id"], "register_node", body.id, commit)
        except DuplicateKeyError as exc:
            raise HTTPException(409, "节点 ID 已登记") from exc
        heartbeat = await repo.db.nodes.find_one({"id": body.id})
        return _public_node_config(document, heartbeat)

    @app.patch("/api/v1/admin/nodes/{node_id}")
    async def update_node_config(node_id: str, body: NodeConfigPatch, request: Request, user: User):
        """通过版本锁修改人工准入设置，不触碰 worker 当前写入的心跳数据。"""
        authorize(user, "admin")
        repo = request.app.state.repo
        changes = body.model_dump(exclude={"version"}, exclude_none=True) | {"updatedAt": now()}

        async def commit(session):
            """版本更新、未命中状态判断和审计均使用同一事务快照。"""
            document = await repo.db.node_configs.find_one_and_update(
                {"id": node_id, "version": body.version, "deletedAt": None},
                {"$set": changes, "$inc": {"version": 1}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if document:
                return document
            if await repo.db.node_configs.find_one({"id": node_id, "deletedAt": None}, session=session):
                raise HTTPException(409, "节点配置已被其他管理员修改，请刷新后重试")
            raise HTTPException(404, "节点尚未登记")

        document = await audited_mutation(repo, user["id"], "update_node_config", node_id, commit)
        heartbeat = await repo.db.nodes.find_one({"id": node_id})
        return _public_node_config(document, heartbeat)

    @app.delete("/api/v1/admin/nodes/{node_id}", status_code=204)
    async def remove_node(node_id: str, request: Request, user: User, version: int = Query(ge=0)):
        """管理员删除空闲节点登记及发现项；保留节点地址和全部历史日志。"""
        authorize(user, "admin")
        await delete_node(request.app.state.repo, user["id"], node_id, version)
