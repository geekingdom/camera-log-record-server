"""封装 MongoDB 公共访问、加密、审计和可重试幂等写入。"""

import hashlib
import json
import logging
from datetime import timedelta

from cryptography.fernet import Fernet
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError, OperationFailure

from camera_logs.common.event_indexes import install_event_indexes
from camera_logs.common.presentation_time import now


def public(document):
    """删除密码、令牌、内部路径和租约字段后返回可公开的文档副本。"""
    if not document:
        return document
    return {
        k: v
        for k, v in document.items()
        if k
        not in {
            "_id",
            "password",
            "passwordEncrypted",
            "passwordHash",
            "authVersion",
            "tokenHash",
            "tokenEncrypted",
            "path",
            "resultPath",
            "rawPath",
            "indexPath",
            "leaseUntil",
            "executionToken",
            "workerInstanceId",
            "outputWriterToken",
            "outputReaders",
            "outputExecutionState",
            "outputWriterNodeId",
            "outputWriterStartedAt",
            "outputWriterClosedAt",
            "outputWriterClosureReason",
            "outputCleanupState",
            "outputCleanupStartedAt",
            "outputCleanupLeaseUntil",
            "outputCleanupToken",
            "outputCleanedAt",
            "resourceMonitorLeaseUntil",
            "resourceMonitorLeaseTaskId",
            "resourceMonitorLeaseRunId",
            "resourceMonitorLeaseGeneration",
            "resourceMonitorLeaseNodeId",
            "resourceMonitorCheckedAt",
            "coredumpCatalogFenceAt",
            "healthLeaseUntil",
            "healthLeaseStartedAt",
            "healthLeaseToken",
            "healthConsumeClaim",
            "resumeClaimToken",
            "resumeClaimExpires",
            "outputRecoveryLeases",
            "slaveSshPort",
            "slaveSshBootstrap",
        }
    }


class Repository:
    """承载数据库实例、Fernet 密码保护和跨 API 的持久化约定。"""

    def __init__(self, db, settings):
        self.db = db
        self.settings = settings
        self.cipher = Fernet(settings.encryption_key.encode())

    async def initialize(self):
        """创建唯一、查询和 TTL 索引；重复调用不会改变已有业务数据。"""
        for name in (
            "tasks",
            "templates",
            "operations",
            "commands",
            "common_commands",
            "nodes",
            "node_configs",
            "platform_settings",
            "files",
            "jobs",
            "tokens",
            "runs",
            "resources",
            "coredump_files",
            "coredump_exports",
            "coredump_scan_state",
            "coredump_snapshot_reservations",
            "coredump_snapshot_claims",
            "coredump_export_reservations",
            "coredump_export_reservation_claims",
            "resource_metric_hours",
        ):
            await self.db[name].create_index("id", unique=True)
        # 旧版本以 name_1 全局唯一，迁移时只替换该精确旧索引，不删除模板数据或其它索引。
        template_indexes = await self.db.templates.index_information()
        legacy_name = template_indexes.get("name_1")
        # 新索引先完成，避免迁移窗口失去唯一约束；多 API/Worker 并发初始化可安全复用它。
        await self.db.templates.create_index([("createdBy", 1), ("name", 1)], unique=True)
        if legacy_name and legacy_name.get("key") == [("name", 1)]:
            try:
                await self.db.templates.drop_index("name_1")
            except OperationFailure as error:
                if error.code != 27:
                    raise
        # 全局快捷命令按文本唯一，普通用户读取只扫描启用记录并按命令稳定排序。
        await self.db.common_commands.create_index("command", unique=True)
        await self.db.common_commands.create_index(
            [("enabled", 1), ("command", 1), ("id", 1)],
            name="common_commands_enabled_command",
        )
        await self.db.common_commands.create_index(
            [("command", 1), ("id", 1)],
            name="common_commands_command",
        )
        await self.db.idempotency.create_index([("actor", 1), ("key", 1)], unique=True)
        await self.db.idempotency.create_index("expiresAt", expireAfterSeconds=0)
        # 每个任务只保留一个活动运行锁；不同任务可以使用同一设备端点。
        await self.db.endpoint_locks.create_index("taskId", unique=True)
        await self.db.files.create_index([("taskId", 1), ("hour", 1)])
        # 旧归档全文索引按节点/READY/稳定ID游标补建，避免每轮扫全部历史目录。
        await self.db.files.create_index([("nodeId", 1), ("status", 1), ("id", 1)])
        await self.db.files.create_index([("nodeId", 1), ("path", 1), ("status", 1), ("id", 1)],
                                         name="files_node_archive_members")
        await self.db.files.create_index(
            [("deploymentClosedProof.nodeId", 1), ("status", 1), ("deploymentClosedProof.archiveRecoveredAt", 1)],
            name="files_deployment_closed_proof_recovery",
            partialFilterExpression={"deploymentClosedProof.token": {"$exists": True}},
        )
        await self.db.log_gap_snapshots.create_index("expiresAt", expireAfterSeconds=0)
        await self.db.coredump_files.create_index([("resourceId", 1), ("receivedAt", -1), ("id", 1)])
        # 资源看板按上海日统计所有可见目录项，时间前缀限制候选集后再按资源聚合。
        await self.db.coredump_files.create_index([("receivedAt", 1), ("resourceId", 1)],
                                                  name="coredump_files_received_resource")
        await self.db.coredump_files.create_index([("nodeId", 1), ("status", 1), ("updatedAt", -1)])
        # 源文件复核按节点和稳定游标限量读取，存在性条件在索引候选内过滤。
        await self.db.coredump_files.create_index([("nodeId", 1), ("id", 1)])
        await self.db.coredump_files.create_index([("sourceKey", 1), ("nodeId", 1), ("version", -1)])
        # 快照孤儿回收按本节点 token 引用核对，避免每个过期 claim 扫描整个 catalog。
        await self.db.coredump_files.create_index([("nodeId", 1), ("freezeToken", 1)])
        await self.db.coredump_files.create_index([("nodeId", 1), ("snapshot.reservationToken", 1)])
        await self.db.resource_metric_hours.create_index([("resourceId", 1), ("bucketStart", -1)])
        await self.db.resource_metric_hours.create_index("expiresAt", expireAfterSeconds=0)
        await self.db.coredump_snapshot_reservations.create_index("id", unique=True)
        await self.db.coredump_snapshot_claims.create_index("id", unique=True)
        await self.db.coredump_snapshot_claims.create_index([("nodeId", 1), ("state", 1), ("expiresAt", 1)])
        await self.db.coredump_export_reservations.create_index("id", unique=True)
        await self.db.coredump_exports.create_index("expiresAt", expireAfterSeconds=0)
        await self.db.tasks.create_index([("desiredState", 1), ("nodeId", 1)])
        await self.db.tasks.create_index([("desiredState", 1), ("nodeId", 1), ("status", 1)])
        # 默认任务列表和调度候选均排除软删除记录，索引覆盖筛选前缀及稳定列表排序。
        await self.db.tasks.create_index([("deletedAt", 1), ("createdAt", -1), ("id", 1)],
                                         name="tasks_visible_created")
        await self.db.tasks.create_index([("deletedAt", 1), ("desiredState", 1), ("nodeId", 1), ("status", 1)],
                                         name="tasks_visible_scheduler")
        # 删除收尾只读取尚待回收连接的软删除任务，避免每个调度周期扫描全部历史任务。
        await self.db.tasks.create_index([("deletedAt", 1), ("deletionState", 1)],
                                         name="tasks_deleted_reconcile")
        await self.db.runs.create_index([("nodeId", 1), ("endedAt", 1)])
        await self.db.runs.create_index([("endedAt", 1), ("id", 1)])
        # 部署 prepare/complete 与旧原始日志恢复只扫描带证明的极少数 run，不能扩大普通运行写入索引。
        await self.db.runs.create_index(
            [("deploymentRecovery.token", 1), ("deploymentRecovery.nodeId", 1)],
            name="runs_deployment_recovery_token_node",
            partialFilterExpression={"deploymentRecovery.token": {"$exists": True}},
        )
        await self.db.run_archives.create_index("runId", unique=True)
        await self.db.audit_archive_chunks.create_index("expiresAt", expireAfterSeconds=0)
        await self.db.audit_archive_chunks.create_index([("day", 1), ("part", 1)], unique=True)
        await self.db.run_archives.create_index([("taskId", 1), ("endedAt", -1)])
        await self.db.commands.create_index([("taskId", 1), ("runId", 1), ("id", 1)])
        await self.db.commands.create_index([("runId", 1), ("status", 1)])
        await self.db.operations.create_index([("createdAt", 1), ("_id", 1)])
        await self.db.operations.create_index([("taskId", 1), ("runId", 1), ("status", 1)])
        await self.db.endpoint_locks.create_index("runId")
        await self.db.idempotency.create_index([("resourceId", 1), ("expiresAt", 1)])
        await self.db.operations.create_index([("desiredState", 1), ("status", 1)])
        await self.db.tasks.create_index("resourceId")
        await self.db.tasks.create_index("serialServerResourceId")
        await self.db.resources.create_index("deletionState")
        # 默认“查看全部”与包含软删除的历史列表均按创建时刻稳定分页，不能依赖内存排序。
        await self.db.resources.create_index(
            [("createdAt", -1), ("id", 1)],
            name="resources_visible_created",
            partialFilterExpression={"deletedAt": None},
        )
        await self.db.resources.create_index(
            [("createdAt", -1), ("id", 1)],
            name="resources_created",
        )
        # “仅查看自己”将创建者和共享协作者作为两个可合并的查询分支；当前资源使用
        # deletedAt:null 部分索引，包含软删除时改用对应完整历史索引。
        await self.db.resources.create_index(
            [("createdBy", 1), ("createdAt", -1), ("id", 1)],
            name="resources_creator_visible_created",
            partialFilterExpression={"deletedAt": None},
        )
        await self.db.resources.create_index(
            [("sharedUserIds", 1), ("createdAt", -1), ("id", 1)],
            name="resources_shared_visible_created",
            partialFilterExpression={"deletedAt": None},
        )
        await self.db.resources.create_index(
            [("createdBy", 1), ("createdAt", -1), ("id", 1)],
            name="resources_creator_created",
        )
        await self.db.resources.create_index(
            [("sharedUserIds", 1), ("createdAt", -1), ("id", 1)],
            name="resources_shared_created",
        )
        # 重添设备只关联IP、型号和序列号全部一致的历史资源，避免逐次扫描全部软删记录。
        await self.db.resources.create_index(
            [("kind", 1), ("ip", 1), ("model", 1), ("subSerialNumber", 1), ("deletedAt", 1), ("id", 1)],
            name="resources_coredump_reassociation_identity",
        )
        # 健康循环短周期只检索网络资源中已到期的候选项，避免扫描全部资源。
        await self.db.resources.create_index([("kind", 1), ("deletedAt", 1), ("nextHealthCheckAt", 1)])
        await self.db.jobs.create_index([("nodeId", 1), ("status", 1), ("leaseUntil", 1)])
        await self.db.jobs.create_index([("status", 1), ("leaseUntil", 1)])
        await self.db.jobs.create_index([("nodeId", 1), ("status", 1), ("createdAt", 1), ("id", 1)])
        # 下载历史按任务和创建时刻稳定分页，kind 前缀排除同集合内的搜索作业。
        await self.db.jobs.create_index([("taskId", 1), ("kind", 1), ("createdAt", -1), ("id", -1)],
                                        name="jobs_task_download_history")
        await self.db.jobs.create_index([("nodeId", 1), ("kind", 1), ("status", 1),
                                         ("outputExecutionState", 1), ("expiresAt", 1), ("id", 1)],
                                        name="jobs_export_cleanup_candidates")
        await self.db.jobs.create_index([("nodeId", 1), ("kind", 1), ("outputCleanupState", 1),
                                         ("outputCleanupLeaseUntil", 1), ("id", 1)])
        await self.db.authentication_records.create_index("id", unique=True)
        await self.db.authentication_records.create_index(
            [("resourceId", 1), ("result", 1), ("identityChanged", 1), ("createdAt", -1), ("id", -1)],
            name="authentication_records_resource_result_identity_time",
        )
        # 认证记录会随周期探测持续累积。四个索引分别覆盖无筛选、仅结果、仅身份
        # 变化和两项筛选；时间及 ID 尾键同时服务稳定倒序游标，避免深页 skip 扫描。
        await self.db.authentication_records.create_index(
            [("resourceId", 1), ("createdAt", -1), ("id", -1)],
        )
        await self.db.authentication_records.create_index(
            [("resourceId", 1), ("result", 1), ("createdAt", -1), ("id", -1)],
            name="authentication_records_resource_result_time",
        )
        await self.db.authentication_records.create_index(
            [("resourceId", 1), ("identityChanged", 1), ("createdAt", -1), ("id", -1)],
            name="authentication_records_resource_identity_time",
        )
        await self.db.authentication_records.create_index("expiresAt", expireAfterSeconds=0,
                                                           name="authentication_records_expires_at")
        await self.db.commands.create_index([("taskId", 1), ("createdAt", -1)])
        await self.db.commands.create_index([("taskId", 1), ("commandId", 1), ("createdAt", -1)])
        await self.db.commands.create_index([("taskId", 1), ("kind", 1), ("createdAt", -1)])
        await self.db.commands.create_index(
            [
                ("taskId", 1),
                ("kind", 1),
                ("status", 1),
                ("runId", 1),
                ("sessionId", 1),
                ("createdAt", 1),
                ("id", 1),
            ]
        )
        await self.db.tokens.create_index("tokenHash", unique=True)
        # 当前会话每次按绑定用户、撤销状态和到期窗口判断 API 文档资格，避免扫描全部服务账号。
        await self.db.tokens.create_index(
            [("userId", 1), ("revoked", 1), ("expiresAt", 1)],
            name="tokens_active_user_expiry",
        )
        await self.db.users.create_index("id", unique=True)
        # 软删除用户保留审计、创建人和旧令牌归属；用户名只在活动账号中唯一，
        # 新建同名账号必须使用新 ID，不能让旧授权或共享关系复活。
        user_indexes = await self.db.users.index_information()
        await self.db.users.create_index(
            "username", unique=True, name="users_active_username",
            partialFilterExpression={"deletedAt": None},
        )
        legacy_username = user_indexes.get("username_1")
        if legacy_username and legacy_username.get("key") == [("username", 1)]:
            try:
                await self.db.users.drop_index("username_1")
            except OperationFailure as error:
                if error.code != 27:
                    raise
        await self.db.user_sessions.create_index("tokenHash", unique=True)
        await self.db.user_sessions.create_index("expiresAt", expireAfterSeconds=0)
        await self.db.login_limits.create_index("expiresAt", expireAfterSeconds=0)
        await self.db.ip_policy.create_index("id", unique=True)
        await self.db.download_sessions.create_index("expiresAt", expireAfterSeconds=0)
        await self.db.download_sessions.create_index("tokenHash", unique=True)
        await install_event_indexes(self.db)

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

    async def audit(self, actor, action, target, *, session=None, event_id=None, metadata=None):
        """追加用户操作审计事件，不在事件中保存敏感请求内容。"""
        from camera_logs.common.request_context import current_request_context

        context = current_request_context()
        document = {
            "actor": actor,
            "action": action,
            "targetId": target,
            "requestId": context.get("requestId"),
            "clientIp": context.get("clientIp"),
            "createdAt": now(),
        }
        if context.get("serviceTokenId"):
            document["serviceTokenId"] = context["serviceTokenId"]
        # 固定事件标识仅由事务调用方提供，用于确认本次提交，不能使用客户端请求 ID 代替。
        if event_id is not None:
            document["_id"] = event_id
        if metadata:
            document["metadata"] = metadata
        await self.db.audit.insert_one(document, session=session)

    async def idem(self, actor, key, route, payload, collection, build):
        """以 actor 和幂等键串行化创建，并识别同键不同载荷冲突。"""
        if not key or len(key) > 128:
            raise HTTPException(422, "必须提供不超过128字符的 Idempotency-Key")
        digest = hashlib.sha256(
            json.dumps([route, payload], sort_keys=True, default=str).encode()
        ).hexdigest()
        existing = await self.db.idempotency.find_one({"actor": actor, "key": key})
        if existing:
            if existing["digest"] != digest:
                raise HTTPException(409, "幂等键已用于不同请求")
            result = await self.db[collection].find_one({"id": existing["resourceId"]})
            if result:
                return result
            if existing.get("state") != "RETRYABLE":
                raise HTTPException(409, "原请求尚未完成或结果未知，未重复执行")
            claimed = await self.db.idempotency.update_one(
                {"actor": actor, "key": key, "state": "RETRYABLE"},
                {"$set": {"state": "PENDING", "updatedAt": now()}},
            )
            if not claimed.modified_count:
                raise HTTPException(409, "另一个重试正在执行")
            return await self._build_idempotent(actor, key, route, collection, existing["resourceId"], build)
        from camera_logs.common.models import new_id

        identifier = new_id()
        try:
            await self.db.idempotency.insert_one(
                {
                    "actor": actor,
                    "key": key,
                    "digest": digest,
                    "resourceId": identifier,
                    "state": "PENDING",
                    "updatedAt": now(),
                    "expiresAt": now() + timedelta(days=7),
                }
            )
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
            await self.db.idempotency.update_one(
                {"actor": actor, "key": key, "resourceId": identifier},
                {"$set": {"state": "RETRYABLE", "updatedAt": now()}},
            )
            raise HTTPException(503, "创建暂时失败，可使用相同幂等键重试") from exc
        await self.audit(actor, route, identifier)
        return result
