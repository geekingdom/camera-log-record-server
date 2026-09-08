"""封装 MongoDB 公共访问、加密、审计和可重试幂等写入。"""

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError


def now():
    """返回统一的 UTC 时间，避免数据库记录混用本地时区。"""
    return datetime.now(UTC)


def public(document):
    """删除密码、令牌、内部路径和租约字段后返回可公开的文档副本。"""
    if not document:
        return document
    return {k: v for k, v in document.items() if k not in {
        "_id", "password", "passwordEncrypted", "tokenHash", "path", "rawPath", "indexPath", "leaseUntil"
    }}


class Repository:
    """承载数据库实例、Fernet 密码保护和跨 API 的持久化约定。"""
    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.cipher = Fernet(settings.encryption_key.encode())

    async def initialize(self):
        """创建唯一、查询和 TTL 索引；重复调用不会改变已有业务数据。"""
        for name in ("tasks", "templates", "operations", "commands", "nodes", "node_configs", "platform_settings", "files", "jobs", "tokens", "runs", "resources"):
            await self.db[name].create_index("id", unique=True)
        await self.db.templates.create_index("name", unique=True)
        await self.db.idempotency.create_index([("actor", 1), ("key", 1)], unique=True)
        await self.db.idempotency.create_index("expiresAt", expireAfterSeconds=0)
        # 每个任务只保留一个活动运行锁；不同任务可以使用同一设备端点。
        await self.db.endpoint_locks.create_index("taskId", unique=True)
        await self.db.files.create_index([("taskId", 1), ("hour", 1)])
        await self.db.tasks.create_index([("desiredState", 1), ("nodeId", 1)])
        await self.db.tasks.create_index("resourceId")
        await self.db.tasks.create_index("serialServerResourceId")
        await self.db.resources.create_index("deletionState")
        await self.db.commands.create_index([("taskId", 1), ("createdAt", -1)])
        await self.db.commands.create_index([("taskId", 1), ("kind", 1), ("status", 1),
                                            ("runId", 1), ("sessionId", 1), ("createdAt", 1), ("id", 1)])
        await self.db.tokens.create_index("tokenHash", unique=True)
        await self.db.download_sessions.create_index("expiresAt", expireAfterSeconds=0)
        await self.db.download_sessions.create_index("tokenHash", unique=True)

    def encrypt(self, password):
        """将设备密码加密后存储；空值保持为空以支持无密码串口。"""
        return self.cipher.encrypt(password.encode()).decode() if password else ""

    def decrypt(self, encrypted):
        """仅供受控连接层恢复设备密码，公开响应不得调用此方法。"""
        return self.cipher.decrypt(encrypted.encode()).decode() if encrypted else ""

    async def get(self, collection, identifier):
        """按公共 id 获取文档，不存在时统一转换为 404。"""
        doc = await self.db[collection].find_one({"id": identifier})
        if doc is None:
            raise HTTPException(404, "记录不存在")
        return doc

    async def audit(self, actor, action, target, *, session=None):
        """追加用户操作审计事件，不在事件中保存敏感请求内容。"""
        await self.db.audit.insert_one({"actor": actor, "action": action, "targetId": target, "createdAt": now()}, session=session)

    async def idem(self, actor, key, route, payload, collection, build):
        """以 actor 和幂等键串行化创建，并识别同键不同载荷冲突。"""
        if not key or len(key) > 128:
            raise HTTPException(422, "必须提供不超过128字符的 Idempotency-Key")
        digest = hashlib.sha256(json.dumps([route, payload], sort_keys=True, default=str).encode()).hexdigest()
        existing = await self.db.idempotency.find_one({"actor": actor, "key": key})
        if existing:
            if existing["digest"] != digest:
                raise HTTPException(409, "幂等键已用于不同请求")
            result = await self.db[collection].find_one({"id": existing["resourceId"]})
            if result:
                return result
            if existing.get("state") != "RETRYABLE":
                raise HTTPException(409, "原请求尚未完成或结果未知，未重复执行")
            claimed = await self.db.idempotency.update_one({"actor": actor, "key": key, "state": "RETRYABLE"},
                {"$set": {"state": "PENDING", "updatedAt": now()}})
            if not claimed.modified_count:
                raise HTTPException(409, "另一个重试正在执行")
            return await self._build_idempotent(actor, key, route, collection, existing["resourceId"], build)
        from camera_logs.common.models import new_id
        identifier = new_id()
        try:
            await self.db.idempotency.insert_one({"actor": actor, "key": key, "digest": digest,
                "resourceId": identifier, "state": "PENDING", "updatedAt": now(), "expiresAt": now() + timedelta(days=7)})
        except DuplicateKeyError:
            return await self.idem(actor, key, route, payload, collection, build)
        return await self._build_idempotent(actor, key, route, collection, identifier, build)

    async def _build_idempotent(self, actor, key, route, collection, identifier, build):
        """执行一次资源创建；基础设施异常标记为可用同键安全重试。"""
        try:
            result = await build(identifier)
        except HTTPException:
            await self.db.idempotency.delete_one({"actor": actor, "key": key, "resourceId": identifier})
            raise
        except Exception as exc:
            logging.getLogger(__name__).exception("创建操作失败 resource=%s route=%s", identifier, route)
            # 已知失败的重试复用同一资源 ID，绝不额外分配第二个资源。
            stored = await self.db[collection].find_one({"id": identifier})
            if stored is not None:
                return stored
            await self.db.idempotency.update_one({"actor": actor, "key": key, "resourceId": identifier},
                {"$set": {"state": "RETRYABLE", "updatedAt": now()}})
            raise HTTPException(503, "创建暂时失败，可使用相同幂等键重试") from exc
        await self.audit(actor, route, identifier)
        return result
