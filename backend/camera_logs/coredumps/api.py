"""平台 coredump 目录、冻结与下载 API；NFS 原文件从不经此层返回。"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Query, Request, Response

from camera_logs.common.audited_mutations import audited_create, audited_mutation
from camera_logs.common.database import now, public
from camera_logs.common.security import actor, authorize
from camera_logs.coredumps.models import CoredumpExportCreate
from camera_logs.logs.api import proxy_file
from camera_logs.logs.download_sessions import download_actor_for, issue_download_ticket


def _view(document: dict[str, Any]) -> dict[str, Any]:
    """输出 catalog 事实，显式屏蔽源、NFS 目录、inode 和快照内部路径。"""
    allowed = {"id", "kind", "status", "resourceId", "nodeId", "name", "size", "receivedAt", "firstSeenAt",
               "sourceModifiedAt", "sourceState", "sourceStableAt", "sourceObservedAt", "sourceUnchangedSince",
               "updatedAt", "version", "createdAt", "expiresAt", "startedAt", "completedAt", "filename", "bytes",
               "etag", "error"}
    return {key: value for key, value in public(document).items() if key in allowed}


def _expired(value: datetime | None) -> bool:
    """兼容 Mongo 驱动返回的朴素 UTC 时间，统一按 UTC 判断导出保留期。"""
    if value is None:
        return False
    return value.replace(tzinfo=UTC) <= now() if value.tzinfo is None else value.astimezone(UTC) <= now()


async def _resource(repo, user, identifier: str) -> dict[str, Any]:
    """coredump 仅关联有效海康资源；共享读取遵循现有有效用户模型。"""
    resource = await repo.get("resources", identifier)
    authorize(user, "logs:read")
    if resource.get("kind") != "HIKVISION_NETWORK":
        raise HTTPException(404, "海康设备资源不存在")
    return resource


def install_coredump_routes(app):
    """安装平台 coredump 路由，下载鉴权、审计和节点代理均沿用日志约束。"""
    User = Annotated[dict, Depends(actor)]
    CoredumpDownloadUser = Annotated[dict, Depends(download_actor_for("coredump"))]
    CoredumpExportDownloadUser = Annotated[dict, Depends(download_actor_for("coredump_export"))]
    def repo():
        return app.state.repo

    @app.get("/api/v1/resources/{resource_id}/coredumps")
    async def list_coredumps(resource_id: str, user: User, page: int = Query(1, ge=1), pageSize: int = Query(50, ge=1, le=100),
                             receivedFrom: str | None = None, receivedTo: str | None = None, name: str | None = Query(None, max_length=256)):
        """按资源、首次发现时间和字面文件名分页；源稳定性与下载快照状态分别返回。"""
        await _resource(repo(), user, resource_id)
        # 历史设备会写入同名标志文件；catalog 保留原记录，列表不把它当作可导出的 coredump。
        query: dict[str, Any] = {
            "resourceId": resource_id,
            "$nor": [{"name": {"$regex": r"(^|/)coredump_flag[.]cdf$", "$options": "i"}}],
        }
        if name:
            import re
            query["name"] = {"$regex": re.escape(name), "$options": "i"}
        try:
            start_value = datetime.fromisoformat(receivedFrom) if receivedFrom else None
            end_value = datetime.fromisoformat(receivedTo) if receivedTo else None
            if (start_value and start_value.tzinfo is None) or (end_value and end_value.tzinfo is None):
                raise ValueError()
            start = start_value.astimezone(UTC) if start_value else None
            end = end_value.astimezone(UTC) if end_value else None
        except ValueError as error:
            raise HTTPException(422, "首次发现时间必须包含时区") from error
        if start and end and start > end:
            raise HTTPException(422, "首次发现时间范围无效")
        if start or end:
            query["receivedAt"] = {key: value for key, value in (("$gte", start), ("$lte", end)) if value}
        cursor = repo().db.coredump_files.find(query).sort([("receivedAt", -1), ("id", 1)]).skip((page - 1) * pageSize).limit(pageSize)
        items = [_view(item) async for item in cursor]
        await repo().audit(user["id"], "list_coredumps", resource_id)
        return {"items": items, "total": await repo().db.coredump_files.count_documents(query), "page": page, "pageSize": pageSize}

    @app.post("/api/v1/coredump-exports", status_code=202)
    async def create_export(body: CoredumpExportCreate, request: Request, user: User):
        """事务化入队，Worker 后台冻结当前版本并汇聚跨节点 ZIP。"""
        authorize(user, "logs:download")
        files = [await repo().get("coredump_files", identifier) for identifier in body.fileIds]
        if len({item["id"] for item in files}) != len(files):
            raise HTTPException(422, "coredump 文件不能重复")
        estimate = sum(int(item.get("size", 0)) for item in files)
        if estimate > int(repo().settings.coredump_export_max_bytes):
            raise HTTPException(413, "coredump 导出超过产物上限")
        for item in files:
            await _resource(repo(), user, item["resourceId"])
        coordinator = files[0]["nodeId"]
        async def prepare(identifier):
            stamp = now()
            return {"id": identifier, "kind": "COREDUMP_EXPORT", "status": "QUEUED", "actor": user["id"],
                    "fileIds": body.fileIds, "sources": [{"id": item["id"], "nodeId": item["nodeId"],
                        "resourceId": item["resourceId"], "version": item["version"], "source": item["source"]} for item in files],
                    "coordinatorNodeId": coordinator, "estimatedBytes": estimate, "createdAt": stamp,
                    "expiresAt": stamp + timedelta(hours=repo().settings.coredump_retention_hours)}
        return _view(await audited_create(repo(), user["id"], request.headers.get("Idempotency-Key"),
                                          "create_coredump_export", body.model_dump(), "coredump_exports", prepare))

    @app.get("/api/v1/coredump-exports/{identifier}")
    async def export(identifier: str, user: User):
        authorize(user, "logs:download")
        document = await repo().get("coredump_exports", identifier)
        if document["actor"] != user["id"] and not user.get("isAdmin"):
            raise HTTPException(403, "仅创建者可读取导出")
        return _view(document)

    @app.post("/api/v1/coredump-exports/{identifier}/browser-session")
    async def export_browser_session(identifier: str, request: Request, response: Response, user: User):
        """签发只作用于该导出内容 URL 的短期浏览器下载 Cookie。"""
        document = await repo().get("coredump_exports", identifier)
        authorize(user, "logs:download")
        if document["actor"] != user["id"] and not user.get("isAdmin"):
            raise HTTPException(403, "仅创建者可下载导出")
        if document.get("status") != "SUCCEEDED" or _expired(document.get("expiresAt")):
            raise HTTPException(409, "coredump 导出尚不可下载")
        token = await issue_download_ticket(repo(), user, identifier, target_type="coredump_export")
        path = f"/api/v1/coredump-exports/{identifier}/content"
        response.set_cookie("download_access", token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                            path=path, max_age=300)
        return {"url": path, "expiresInSeconds": 300}

    @app.post("/api/v1/coredumps/{identifier}/browser-session")
    async def coredump_browser_session(identifier: str, request: Request, response: Response, user: User):
        """单固定副本同样使用路径受限票据，禁止前端 Blob 中转。"""
        document = await repo().get("coredump_files", identifier)
        await _resource(repo(), user, document["resourceId"])
        authorize(user, "logs:download")
        if document.get("status") != "FROZEN" or not document.get("snapshot"):
            raise HTTPException(409, "coredump 尚未冻结")
        token = await issue_download_ticket(repo(), user, identifier, target_type="coredump")
        path = f"/api/v1/coredumps/{identifier}/content"
        response.set_cookie("download_access", token, httponly=True, samesite="strict", secure=request.url.scheme == "https",
                            path=path, max_age=300)
        return {"url": path, "expiresInSeconds": 300}

    @app.delete("/api/v1/coredump-exports/{identifier}", status_code=204)
    async def cancel_export(identifier: str, user: User):
        authorize(user, "logs:download")
        document = await repo().get("coredump_exports", identifier)
        if document["actor"] != user["id"] and not user.get("isAdmin"):
            raise HTTPException(403, "仅创建者可取消导出")
        async def cancel(session):
            await repo().db.coredump_exports.update_one({"id": identifier, "status": {"$in": ["QUEUED", "RUNNING"]}},
                {"$set": {"status": "CANCELLED", "updatedAt": now()}}, session=session)
        await audited_mutation(repo(), user["id"], "cancel_coredump_export", identifier, cancel)

    @app.get("/api/v1/coredump-exports/{identifier}/content")
    async def export_content(identifier: str, request: Request, user: CoredumpExportDownloadUser):
        document = await repo().get("coredump_exports", identifier)
        authorize(user, "logs:download")
        if document["actor"] != user["id"] and not user.get("isAdmin"):
            raise HTTPException(403, "仅创建者可下载导出")
        if document.get("status") != "SUCCEEDED" or _expired(document.get("expiresAt")):
            raise HTTPException(409, "coredump 导出尚未完成")
        await repo().audit(user["id"], "download_coredump_export", identifier)
        return await proxy_file(repo(), document["coordinatorNodeId"], f"/internal/coredump-exports/{identifier}/content",
                                request.headers.get("range"), request.headers.get("if-range"))

    @app.get("/api/v1/coredumps/{identifier}/content")
    async def coredump_content(identifier: str, request: Request, user: CoredumpDownloadUser):
        """单文件仅代理固定版本，Range/If-Range 交由持有副本的节点处理。"""
        document = await repo().get("coredump_files", identifier)
        await _resource(repo(), user, document["resourceId"])
        authorize(user, "logs:download")
        if document.get("status") != "FROZEN" or not document.get("snapshot"):
            raise HTTPException(409, "coredump 尚未冻结，请创建导出以固定当前版本")
        await repo().audit(user["id"], "download_coredump", identifier)
        return await proxy_file(repo(), document["nodeId"], f"/internal/coredumps/{identifier}/content",
                                request.headers.get("range"), request.headers.get("if-range"))
