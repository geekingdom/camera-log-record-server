"""提供海康网络资源的受限批量创建，认证完成后才以单事务写入全部结果。"""

import asyncio
import logging
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from pymongo.errors import DuplicateKeyError, PyMongoError
from pymongo.read_concern import ReadConcern
from pymongo.read_preferences import ReadPreference

from camera_logs.common import audited_mutations
from camera_logs.common.api_contracts.response_schema import batch_authentication_error_schema
from camera_logs.common.api_errors import api_error_response_for_schema
from camera_logs.common.database import now, public
from camera_logs.common.models import new_id
from camera_logs.common.security import actor, authorize
from camera_logs.coredumps.resource_identity import reassociate_coredump_history
from camera_logs.resources.address_claim import claim_address
from camera_logs.resources.authentication import DeviceOfflineError, authenticate_network_resource
from camera_logs.resources.authentication_records import record_authentication
from camera_logs.resources.models import ResourceBatchInput, ResourceInput
from camera_logs.resources.sharing import validate_share_users
from camera_logs.users.write_gate import claim_active_user_for_resource_write

logger = logging.getLogger(__name__)
_AUTHENTICATION_CONCURRENCY = 4


def _failure_code(error: Exception) -> str:
    """把设备探测异常压缩为可供逐项显示的安全类别，避免回显凭据或设备正文。"""
    if isinstance(error, PermissionError):
        return "AUTH_FAILED"
    if isinstance(error, DeviceOfflineError):
        return "OFFLINE"
    return "DEVICE_ERROR"


def _majority_primary_database(repo):
    """与单资源创建相同地只从多数确认主库读取幂等成功映射。"""
    return repo.db.with_options(
        read_concern=ReadConcern("majority"), read_preference=ReadPreference.PRIMARY,
    )


async def _authenticate_item(index: int, item: ResourceInput, semaphore: asyncio.Semaphore):
    """在有界并发内完成一次设备认证，失败只保留定位所需的索引、IP和类别。"""
    try:
        async with semaphore:
            metadata = await authenticate_network_resource(
                ip=item.ip, username=item.username, password=item.password, auth_type=item.authType,
            )
        return index, item, metadata, None
    except Exception as error:  # noqa: BLE001 - 单台探测异常必须阻止整批提交，错误正文不得返回。
        return index, item, None, {"index": index, "ip": item.ip, "code": _failure_code(error)}


async def _confirmed_items(database, digest: str, actor_id: str, key: str, *, session=None):
    """只接受同键完整成功映射；未确认状态绝不再次认证或写入。"""
    existing = await database.idempotency.find_one({"actor": actor_id, "key": key}, session=session)
    if existing is None:
        return None
    if existing.get("digest") != digest:
        raise HTTPException(409, "幂等键已用于不同请求")
    if existing.get("state") != "SUCCEEDED":
        raise HTTPException(409, "原请求尚未完成或结果未知，未重复执行")
    identifiers = existing.get("resourceIds")
    if not isinstance(identifiers, list) or not identifiers:
        raise HTTPException(409, "原批量请求结果无效")
    documents = []
    for identifier in identifiers:
        document = await database.resources.find_one({"id": identifier}, session=session)
        if document is None or document.get("deletedAt") is not None:
            raise HTTPException(410, "原请求创建的资源已删除")
        documents.append(document)
    return documents


async def _recover_confirmed(repo, digest: str, actor_id: str, key: str):
    """事务异常后仅从多数可见主库恢复已确认成功，避免把未知提交误作可重试。"""
    try:
        database = _majority_primary_database(repo)
        return await _confirmed_items(database, digest, actor_id, key)
    except PyMongoError:
        return None


def _resource_document(repo, identifier: str, item: ResourceInput, metadata: dict, actor_id: str, actor_name: str):
    """把已认证输入转为待提交资源；凭据仅在事务内写入加密字段。"""
    timestamp = now()
    return {
        "id": identifier, "name": item.name, "kind": item.kind, "ip": item.ip, "version": 1,
        "createdAt": timestamp, "updatedAt": timestamp, "createdBy": actor_id, "createdByName": actor_name,
        "sharedUserIds": list(dict.fromkeys(item.sharedUserIds)),
        "enableCoredumpMonitor": item.enableCoredumpMonitor,
        "enableResourceMonitor": item.enableResourceMonitor, "username": item.username, "authType": item.authType,
        "passwordEncrypted": repo.encrypt(item.password),
        "authenticatedAt": timestamp, "healthStatus": "ONLINE", "healthCheckedAt": timestamp,
        "healthFailureCount": 0, "nextHealthCheckAt": timestamp + timedelta(seconds=60), **metadata,
    }


def install_batch_creation_route(app, repo):
    """在动态资源 ID 路由前注册静态批量入口，避免被 ``/{identifier}`` 捕获。"""
    User = Annotated[dict, Depends(actor)]

    @app.post("/api/v1/resources/batch", status_code=201, description='批量创建1至256个海康网络设备资源，需要resources:create与Idempotency-Key。逐项重新认证，全部成功后原子提交；重复IP、已有资源IP或任一认证失败均不部分创建。同一成功请求使用相同幂等键重试返回原批次；密码不通过创建响应回传。')
    async def create_resources_batch(body: ResourceBatchInput, request: Request, user: User):
        """认证全部成功后原子创建资源、地址占用、初始认证历史和逐资源审计。"""
        authorize(user, "resources:create")
        key = request.headers.get("Idempotency-Key")
        if not key or len(key) > 128:
            raise HTTPException(422, "必须提供不超过128字符的 Idempotency-Key")
        digest = audited_mutations.request_digest("create_resources_batch", body.model_dump())
        existing = await _confirmed_items(_majority_primary_database(repo()), digest, user["id"], key)
        if existing is not None:
            return {"items": [public(item) for item in existing]}

        semaphore = asyncio.Semaphore(_AUTHENTICATION_CONCURRENCY)
        checks = await asyncio.gather(*[
            _authenticate_item(index, item, semaphore) for index, item in enumerate(body.items)
        ])
        failures = [failure for _, _, _, failure in checks if failure is not None]
        if failures:
            for failure in failures:
                action = "authenticate_resource_credentials_rejected" if failure["code"] == "AUTH_FAILED" else \
                    "authenticate_resource_device_error"
                await repo().audit(user["id"], action, f"ip:{failure['ip']}")
            return api_error_response_for_schema(
                422, "BATCH_AUTHENTICATION_FAILED", "批量设备认证失败",
                getattr(request.state, "request_id", None), batch_authentication_error_schema(),
                details={"items": failures},
            )
        actor_name = user.get("displayName") or user.get("username") or user["id"]
        documents = [_resource_document(repo(), new_id(), item, metadata, user["id"], actor_name)
                     for _, item, metadata, _ in checks]

        async def commit(session):
            """重试事务仅执行数据库操作，地址、认证历史和审计随任一冲突整体回滚。"""
            previous = await _confirmed_items(repo().db, digest, user["id"], key, session=session)
            if previous is not None:
                return previous
            await claim_active_user_for_resource_write(repo().db, user["id"], session)
            for document in documents:
                await validate_share_users(repo(), document.get("sharedUserIds", []), session=session)
                await claim_address(repo(), document, session)
            timestamp = now()
            await repo().db.idempotency.insert_one({
                "actor": user["id"], "key": key, "digest": digest, "resourceId": documents[0]["id"],
                "resourceIds": [document["id"] for document in documents], "state": "SUCCEEDED",
                "updatedAt": timestamp, "expiresAt": timestamp + timedelta(days=7),
            }, session=session)
            await repo().db.resources.insert_many(documents, session=session)
            for document in documents:
                await record_authentication(repo(), document, source="CREATE", result="SUCCESS", after=document,
                                            completed_at=document["authenticatedAt"], session=session)
                await reassociate_coredump_history(repo(), document, session=session)
                await repo().audit(user["id"], "create_resource", document["id"], session=session)
            return documents

        try:
            created = await audited_mutations.mutation_transaction(repo(), commit)
        except DuplicateKeyError as error:
            confirmed = await _recover_confirmed(repo(), digest, user["id"], key)
            if confirmed is not None:
                return {"items": [public(item) for item in confirmed]}
            raise HTTPException(409, "资源地址已存在或幂等键冲突") from error
        except HTTPException:
            raise
        except asyncio.CancelledError:
            raise
        except PyMongoError as error:
            confirmed = await _recover_confirmed(repo(), digest, user["id"], key)
            if confirmed is not None:
                return {"items": [public(item) for item in confirmed]}
            logger.warning("批量资源创建提交结果未知 actor=%s", user["id"])
            raise HTTPException(503, "创建提交结果未知，请使用相同幂等键重试") from error
        return {"items": [public(item) for item in created]}
