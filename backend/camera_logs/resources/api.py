"""提供设备资源的认证预览、保存和受限读取接口。"""

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from pydantic import IPvAnyAddress
from pymongo import ReturnDocument

from camera_logs.common.audited_mutations import audited_create, audited_mutation
from camera_logs.common.database import now, public
from camera_logs.common.security import actor, authorize, authorize_owner
from camera_logs.resources.address_claim import claim_address, release_address
from camera_logs.resources.authentication import DeviceOfflineError, authenticate_network_resource
from camera_logs.resources.authentication_records import (
    after_cursor_clause,
    decode_cursor,
    encode_cursor,
    record_authentication,
)
from camera_logs.resources.health import apply_manual_result, grant_after_user_authentication
from camera_logs.resources.lifecycle import reconcile_resource_deletion, task_resource_query
from camera_logs.resources.models import CoredumpMonitorStatus, ResourceInput, ResourcePatch


def _resource_public(document):
    """统一移除连接密文和内部认证时间以外的敏感资源字段。"""
    return public(document)


def _lease_expired(value, timestamp) -> bool:
    """兼容测试替身返回的 naive UTC 时间；正式 Mongo 客户端始终返回带时区时间。"""
    if value is None:
        return True
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value <= timestamp


async def _resource_view(repo, document, user: dict | None = None, task_limit: int = 100):
    """返回有界任务列表，以及用于展示采集和删除风险的两类独立摘要。"""
    item = _resource_public(document)
    task_query = task_resource_query(item["id"])
    item["taskCount"] = await repo().db.tasks.count_documents(task_query)
    item["activeTaskCount"] = await repo().db.tasks.count_documents({"$and": [task_query, {
        "status": "COLLECTING", "desiredState": "RUNNING",
    }]})
    item["unsettledTaskCount"] = await repo().db.tasks.count_documents({"$and": [task_query, {"$or": [
        {"nodeId": {"$exists": True, "$ne": None}}, {"desiredState": {"$in": ["RUNNING", "PAUSED"]}},
        {"status": "BLOCKED"},
    ]}]})
    fields = {key: 1 for key in ("id", "name", "protocol", "ip", "port", "status", "desiredState",
                                 "resourceId", "serialServerResourceId", "createdBy", "createdByName")}
    item["tasks"] = [public(task) async for task in repo().db.tasks.find(task_query, fields).sort("id", 1).limit(task_limit)]
    item["tasksTruncated"] = item["taskCount"] > len(item["tasks"])
    item["tasksUrl"] = f"/api/v1/tasks?resourceId={item['id']}"
    return item


async def _verified_metadata(body: ResourceInput) -> dict[str, str]:
    """仅网络资源需要实时认证；串口服务器没有可探测的海康设备身份。"""
    if body.kind == "SERIAL_SERVER":
        return {}
    try:
        return await authenticate_network_resource(
            ip=body.ip, username=body.username, password=body.password, auth_type=body.authType,
        )
    except PermissionError as exc:
        raise HTTPException(401, str(exc)) from exc
    except DeviceOfflineError as exc:
        raise HTTPException(503, str(exc)) from exc
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(502, str(exc)) from exc


def install_resource_routes(app, repo, listing):
    """注册资源路由，复用统一令牌鉴权、分页和审计约定。"""
    User = Annotated[dict, Depends(actor)]

    async def authenticated_preview_metadata(body: ResourceInput, user: dict, target: str) -> dict[str, str]:
        """执行网络设备认证并记录失败类别，成功事件须待调用方批准身份后写入。"""
        try:
            return await _verified_metadata(body)
        except HTTPException as exc:
            action = "authenticate_resource_credentials_rejected" if exc.status_code == 401 else \
                "authenticate_resource_device_error"
            await repo().audit(user["id"], action, target)
            raise

    async def record_manual_authentication(resource: dict, user: dict, *, result: str,
                                           before: dict, after: dict, message: str | None = None) -> None:
        """将手动认证历史和操作审计作为一个提交单元保存。

        设备 HTTP 请求已经在事务外完成。这里仅提交确定的认证结果；认证历史的头
        记录、明细和审计事件必须共用会话，避免审计失败时留下不可追溯的认证结果。
        """
        action = "authenticate_resource_succeeded" if result == "SUCCESS" else (
            "authenticate_resource_credentials_rejected" if result == "AUTH_FAILED"
            else "authenticate_resource_device_error"
        )

        async def commit(session):
            await record_authentication(
                repo(), resource, source="MANUAL", result=result, before=before, after=after,
                message=message, session=session,
            )

        await audited_mutation(repo(), user["id"], action, resource["id"], commit)

    @app.get("/api/v1/resources")
    async def resources(user: User, page: int = Query(1, ge=1), pageSize: int = Query(20, ge=1, le=100),
                        kind: str | None = None, search: str | None = Query(None, max_length=256),
                        createdBy: str | None = Query(None, max_length=128),
                        includeDeleted: bool = False, taskLimit: int = Query(100, ge=1, le=500),
                        name: str | None = Query(None, max_length=128), ip: IPvAnyAddress | None = None,
                        model: str | None = Query(None, max_length=256),
                        subSerialNumber: str | None = Query(None, max_length=256),
                        softwareVersion: str | None = Query(None, max_length=256)):
        """资源发现：search跨名称/IP/型号/序列号/软件版本搜索；独立字段取交集，IP精确匹配。

        返回授权范围内任务摘要，默认最多100条；tasksTruncated为true时使用tasksUrl分页读取。
        createdBy 是创建用户 ID 的精确筛选，用于工作台“仅看自己”和创建用户目录筛选。
        除IP外均按不区分大小写的字面子串匹配，不解释正则表达式。
        """
        authorize(user, "tasks:read")
        query = {} if includeDeleted else {"deletedAt": None}
        if kind:
            query["kind"] = kind
        if createdBy:
            query["createdBy"] = createdBy
        if search:
            query["$or"] = [{field: {"$regex": re.escape(search), "$options": "i"}}
                            for field in ("name", "ip", "model", "subSerialNumber", "softwareVersion")]
        for field, value in (("name", name), ("model", model), ("subSerialNumber", subSerialNumber),
                             ("softwareVersion", softwareVersion)):
            if value:
                query[field] = {"$regex": re.escape(value), "$options": "i"}
        if ip is not None:
            query["ip"] = str(ip)
        result = await listing("resources", query, page, pageSize)
        result["items"] = [await _resource_view(repo, item, user, taskLimit) for item in result["items"]]
        return result

    @app.get("/api/v1/resources/{identifier}")
    async def get_resource(identifier: str, user: User, taskLimit: int = Query(100, ge=1, le=500)):
        """所有有效用户共享资源详情，写权限单独按创建者判断。"""
        authorize(user, "tasks:read")
        return await _resource_view(repo, await repo().get("resources", identifier), user, taskLimit)

    @app.get("/api/v1/resources/{identifier}/authentication-records")
    async def authentication_records(identifier: str, user: User, page: int = Query(1, ge=1),
                                     pageSize: int = Query(20, ge=1, le=100), result: str | None = None,
                                     start: str | None = None, end: str | None = None,
                                     identityChanged: bool | None = None, cursor: str | None = Query(None, max_length=1024)):
        """分页读取认证历史；游标路径避免长期记录翻深页时扫描并丢弃前页结果。"""
        authorize(user, "tasks:read")
        await repo().get("resources", identifier)
        cursor_supplied = cursor is not None
        if cursor_supplied and page != 1:
            raise HTTPException(422, "使用认证记录游标时 page 必须为 1")
        query = {"resourceId": identifier}
        if result:
            if result not in {"SUCCESS", "AUTH_FAILED", "OFFLINE", "ERROR"}:
                raise HTTPException(422, "认证结果筛选无效")
            query["result"] = result
        if identityChanged is not None:
            query["identityChanged"] = identityChanged
        retention_days = int(getattr(repo().settings, "authentication_record_retention_days", 90))
        if retention_days > 0:
            visible_after = now() - timedelta(days=retention_days)
            # TTL 监控器异步删除。接口先排除已过期记录，旧记录未写 expiresAt 时按
            # createdAt 回退，避免清理窗口内向用户短暂展示超过保留期的认证历史。
            query = {"$and": [query, {"$or": [
                {"expiresAt": {"$gte": now()}},
                {"expiresAt": {"$exists": False}, "createdAt": {"$gte": visible_after}},
                {"expiresAt": None, "createdAt": {"$gte": visible_after}},
            ]}]}
        if start or end:
            if not start or not end:
                raise HTTPException(422, "开始和结束时间必须同时提供")
            try:
                lower, upper = datetime.fromisoformat(start), datetime.fromisoformat(end)
            except ValueError as error:
                raise HTTPException(422, "时间必须使用 ISO 8601") from error
            if lower.tzinfo is None or upper.tzinfo is None or upper <= lower:
                raise HTTPException(422, "时间范围必须带时区且结束晚于开始")
            lower, upper = lower.astimezone(UTC), upper.astimezone(UTC)
            # 聚合记录覆盖 [createdAt, latestAt] 区间；区间与查询范围相交即返回。
            # 历史未迁移记录没有 latestAt，按单次 createdAt 作为结束时间回退。
            query = {"$and": [query, {"createdAt": {"$lt": upper}}, {"$or": [
                {"latestAt": {"$gte": lower}},
                {"latestAt": {"$exists": False}, "createdAt": {"$gte": lower}},
                {"latestAt": None, "createdAt": {"$gte": lower}},
            ]}]}
        if cursor:
            created_at, last_id = decode_cursor(cursor, identifier)
            query = {"$and": [query, after_cursor_clause(created_at, last_id)]}
        database_cursor = repo().db.authentication_records.find(
            query, {"_id": 0, "historyFirstRevision": 0, "historyLatestRevision": 0}
        ).sort(
            [("createdAt", -1), ("id", -1)]
        )
        if cursor_supplied:
            # 多取一条仅用于判断是否还有下一页，游标请求不执行全量 count_documents。
            records = [item async for item in database_cursor.limit(pageSize + 1)]
            has_more = len(records) > pageSize
            records = records[:pageSize]
            return {"items": [public(item) for item in records], "total": None, "page": 1,
                    "pageSize": pageSize, "hasMore": has_more,
                    "nextCursor": encode_cursor(records[-1]) if has_more else None}
        total = await repo().db.authentication_records.count_documents(query)
        records = [public(item) async for item in database_cursor.skip((page - 1) * pageSize).limit(pageSize)]
        return {"items": records, "total": total, "page": page, "pageSize": pageSize,
                "nextCursor": encode_cursor(records[-1]) if len(records) == pageSize else None}

    @app.get("/api/v1/resources/{identifier}/coredump-monitor", response_model=CoredumpMonitorStatus)
    async def coredump_monitor_status(identifier: str, user: User):
        """返回当前有效的资源级 Coredump owner，不把任务配置变成永久资源占用。"""
        authorize(user, "tasks:read")
        resource = await repo().get("resources", identifier)
        timestamp = now()
        inactive = {"active": False, "ownerTask": None, "mountStatus": None}
        if (resource.get("kind") != "HIKVISION_NETWORK" or not resource.get("enableCoredumpMonitor", False)
                or resource.get("deletedAt") is not None
                or resource.get("healthStatus") != "ONLINE"
                or _lease_expired(resource.get("coredumpLeaseUntil"), timestamp)):
            return inactive
        owner = await repo().db.tasks.find_one({
            "id": resource.get("coredumpLeaseTaskId"), "resourceId": identifier,
            "runId": resource.get("coredumpLeaseRunId"), "generation": resource.get("coredumpLeaseGeneration"),
            "nodeId": resource.get("coredumpLeaseNodeId"), "$or": [
                {"protocol": "TELNET_DEVICE"},
                {"protocol": "SSH", "$or": [{"sshTarget": {"$exists": False}}, {"sshTarget": "HOST"}]},
            ],
            "desiredState": "RUNNING", "status": "COLLECTING", "resourceDeleted": {"$ne": True},
        }, {"id": 1, "name": 1, "coredumpMountStatus": 1, "coredumpMountRunId": 1})
        if owner is None:
            return inactive
        node = await repo().db.nodes.find_one({
            "id": resource.get("coredumpLeaseNodeId"), "heartbeat": {"$gte": timestamp - timedelta(seconds=15)},
        }, {"id": 1})
        if node is None:
            return inactive
        return {"active": True, "ownerTask": {"id": owner["id"], "name": owner.get("name", "")},
                "mountStatus": owner.get("coredumpMountStatus")
                if owner.get("coredumpMountRunId") == resource.get("coredumpLeaseRunId") else None}

    @app.post("/api/v1/resources/authenticate")
    async def authenticate_resource(body: ResourceInput, user: User):
        """返回网络设备认证预览，结果只来自本次服务端设备请求。"""
        authorize(user, "resources:create")
        if body.kind == "SERIAL_SERVER":
            return {}
        metadata = await authenticated_preview_metadata(body, user, f"ip:{body.ip}")
        await repo().audit(user["id"], "authenticate_resource_succeeded", f"ip:{body.ip}")
        return metadata

    @app.post("/api/v1/resources", status_code=201)
    async def create_resource(body: ResourceInput, request: Request, user: User):
        """重新认证网络设备后保存密文凭据，客户端不能提交设备元数据。"""
        authorize(user, "resources:create")
        async def prepare(identifier):
            """认证和加密只执行在事务外，Mongo 驱动重试不会重新请求设备。"""
            if await repo().db.resources.find_one({"ip": body.ip, "deletedAt": None}):
                raise HTTPException(409, "该网络地址已存在设备资源，请使用已有资源")
            metadata = await _verified_metadata(body)
            document = {"id": identifier, "name": body.name, "kind": body.kind, "ip": body.ip,
                        "version": 1, "createdAt": now(), "updatedAt": now(),
                        "createdBy": user["id"], "createdByName": user.get("displayName") or user.get("username") or user["id"],
                        "enableCoredumpMonitor": body.enableCoredumpMonitor,
                        "enableResourceMonitor": body.enableResourceMonitor}
            if body.kind == "HIKVISION_NETWORK":
                document.update(username=body.username, authType=body.authType,
                                passwordEncrypted=repo().encrypt(body.password), authenticatedAt=now(), **metadata)
            return document

        async def reserve(document, session):
            await claim_address(repo(), document, session)
            if document["kind"] == "HIKVISION_NETWORK":
                await record_authentication(repo(), document, source="CREATE", result="SUCCESS", after=document,
                                            completed_at=document["authenticatedAt"], session=session)

        result = await audited_create(repo(), user["id"], request.headers.get("Idempotency-Key"), "create_resource",
                                      body.model_dump(), "resources", prepare, before_insert=reserve)
        return await _resource_view(repo, result, user)

    @app.post("/api/v1/resources/{identifier}/authenticate")
    async def authenticate_existing_resource(identifier: str, user: User, body: ResourceInput | None = None):
        """认证已有资源；空请求体只使用服务端已保存的密文凭据。"""
        authorize(user, "resources:write")
        old = await repo().get("resources", identifier)
        authorize_owner(user, old)
        if old.get("deletedAt"):
            raise HTTPException(409, "资源地址、类型已变化或资源已删除")
        saved_credentials = body is None
        if saved_credentials:
            # 空请求体的结果会停止或授权恢复关联任务，必须具备任务控制权限。
            authorize(user, "tasks:control")

            async def authorize_tasks(session):
                """事务内再次拒绝非管理员影响认证期间新增的他人关联任务。"""
                if user.get("isAdmin") or "*" in user["scopes"]:
                    return
                foreign = {"$and": [task_resource_query(identifier), {"createdBy": {"$ne": user["id"]}}]}
                if await repo().db.tasks.find_one(foreign, session=session):
                    raise HTTPException(403, "资源关联其他用户创建的任务，仅管理员可立即认证")

        if body is None:
            if old["kind"] == "SERIAL_SERVER":
                return {}
            body = ResourceInput(
                name=old["name"], kind=old["kind"], ip=old["ip"], username=old["username"],
                password=repo().decrypt(old.get("passwordEncrypted", "")), authType=old["authType"],
                enableCoredumpMonitor=old.get("enableCoredumpMonitor", False),
                enableResourceMonitor=old.get("enableResourceMonitor", False),
            )
        if body.ip != old["ip"] or body.kind != old["kind"]:
            raise HTTPException(409, "资源地址、类型已变化或资源已删除")
        if old["kind"] == "SERIAL_SERVER":
            return {}
        try:
            metadata = await _verified_metadata(body)
        except HTTPException as error:
            result = "AUTH_FAILED" if error.status_code == 401 else "OFFLINE" if error.status_code == 503 else "ERROR"
            if saved_credentials:
                await apply_manual_result(repo(), old | {"manualHealthResult": True},
                                          status=result, metadata={}, actor=user["id"], authorize_tasks=authorize_tasks)
            else:
                await record_manual_authentication(old, user, result=result, before=old, after=old, message=result)
            raise
        if saved_credentials:
            await apply_manual_result(repo(), old | {"manualHealthResult": True},
                                      status="SUCCESS", metadata=metadata, actor=user["id"], authorize_tasks=authorize_tasks)
        else:
            await record_manual_authentication(old, user, result="SUCCESS", before=old, after=old | metadata)
        return metadata

    @app.patch("/api/v1/resources/{identifier}")
    async def edit_resource(identifier: str, body: ResourcePatch, user: User):
        """以版本条件更新名称、HTTP 凭据和认证身份，资源 IP 与类型不可修改。"""
        authorize(user, "resources:write")
        old = await repo().get("resources", identifier)
        authorize_owner(user, old)
        if old.get("deletedAt") is not None:
            raise HTTPException(409, "资源已删除，不能编辑")
        if old.get("version") != body.version:
            raise HTTPException(409, "资源版本已变化，请刷新")
        if body.ip != old["ip"] or body.kind != old["kind"]:
            raise HTTPException(422, "资源 IP 和类型不可编辑；请新建资源")
        password = body.password or repo().decrypt(old.get("passwordEncrypted", ""))
        try:
            checked = ResourceInput(name=body.name, kind=body.kind, ip=body.ip, username=body.username,
                                    password=password, authType=body.authType,
                                    enableCoredumpMonitor=body.enableCoredumpMonitor,
                                    enableResourceMonitor=body.enableResourceMonitor)
        except ValueError as exc:
            raise HTTPException(422, "资源配置无效") from exc
        try:
            metadata = await _verified_metadata(checked)
        except HTTPException as error:
            result = "AUTH_FAILED" if error.status_code == 401 else "OFFLINE" if error.status_code == 503 else "ERROR"
            await record_authentication(repo(), old, source="EDIT", result=result, before=old, after=old,
                                        message=result)
            raise
        update = {"name": checked.name, "updatedAt": now(),
                  "enableCoredumpMonitor": checked.enableCoredumpMonitor,
                  "enableResourceMonitor": checked.enableResourceMonitor}
        if checked.kind == "HIKVISION_NETWORK":
            update.update(username=checked.username, authType=checked.authType,
                          passwordEncrypted=repo().encrypt(password), authenticatedAt=now(),
                          healthStatus="ONLINE", healthCheckedAt=now(), healthFailureCount=0,
                          nextHealthCheckAt=now() + timedelta(seconds=60), **metadata)
        async def commit(session):
            """版本 CAS 与审计共享会话；凭据和设备元信息已在事务外准备。"""
            changed = await repo().db.resources.find_one_and_update(
                {"id": identifier, "version": body.version, "deletedAt": None},
                {"$set": update, "$inc": {"version": 1, **({"healthRevision": 1} if checked.kind == "HIKVISION_NETWORK" else {})}},
                return_document=ReturnDocument.AFTER,
                session=session,
            )
            if not changed:
                raise HTTPException(409, "资源版本已变化或已删除，请刷新")
            if checked.kind == "HIKVISION_NETWORK":
                await grant_after_user_authentication(
                    repo(), changed, session,
                    identity_changed=(changed.get("model"), changed.get("subSerialNumber"))
                    != (old.get("model"), old.get("subSerialNumber")),
                )
                await record_authentication(repo(), changed, source="EDIT", result="SUCCESS", before=old,
                                            after=changed, session=session)
            return changed

        changed = await audited_mutation(repo(), user["id"], "edit_resource", identifier, commit)
        return await _resource_view(repo, changed, user)

    @app.delete("/api/v1/resources/{identifier}", status_code=202)
    async def delete_resource(identifier: str, user: User, version: int = Query(ge=1)):
        """软删除资源并请求所有关联任务受控停止，保留任务、文件和历史目录。"""
        authorize(user, "resources:write")
        authorize(user, "tasks:control")
        old = await repo().get("resources", identifier)
        authorize_owner(user, old)
        if old.get("deletedAt") is not None:
            result = await reconcile_resource_deletion(repo(), identifier)
            return await _resource_view(repo, result or old, user)
        deleted_at = now()

        async def commit(session):
            """先提交删除意图及审计，停止任务的可重试扫尾不能扩大事务时长。"""
            if not user.get("isAdmin") and "*" not in user["scopes"]:
                foreign = {"$and": [task_resource_query(identifier), {"createdBy": {"$ne": user["id"]}}]}
                if await repo().db.tasks.find_one(foreign, session=session):
                    raise HTTPException(403, "资源关联其他用户创建的任务，仅管理员可删除")
            changed = await repo().db.resources.find_one_and_update(
                {"id": identifier, "version": version, "deletedAt": None},
                {"$set": {"deletedAt": deleted_at, "deletionState": "PENDING", "updatedAt": deleted_at},
                 "$inc": {"version": 1}},
                return_document=ReturnDocument.AFTER, session=session,
            )
            if not changed:
                raise HTTPException(409, "资源版本已变化，请刷新")
            await release_address(repo(), changed, session)
            return changed

        changed = await audited_mutation(repo(), user["id"], "delete_resource", identifier, commit)
        completed = await reconcile_resource_deletion(repo(), identifier)
        return await _resource_view(repo, completed or changed, user)
