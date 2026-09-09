"""日志目录、字节读取、搜索导出作业和可恢复的实时订阅接口。

正文保留在采集节点，由此层鉴权后转发；导出冻结文件清单与字节水位，避免
查询执行期间的小时轮转改变结果。实时推送允许显式缺口，但不能影响归档保存。
"""
import asyncio
import logging
from datetime import date as CalendarDate
from datetime import datetime, timedelta
from time import perf_counter
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from camera_logs.access_policy.policy import apply_ip_permissions
from camera_logs.common.database import now, public
from camera_logs.common.models import DownloadCreate, SearchCreate
from camera_logs.common.security import actor, authenticate, authorize
from camera_logs.logs.download_sessions import download_actor
from camera_logs.logs.hour_catalog import summarize_hours
from camera_logs.logs.order import ordered_files
from camera_logs.users.sessions import COOKIE, check_origin, session_identity

logger = logging.getLogger(__name__)


async def node_request(repo, node_id, path, params=None, *, client, downstream=None):
    """通过内部令牌请求节点 JSON 数据，将网络故障转换为可重试服务错误。"""
    started = perf_counter()
    node = await repo.get("nodes", node_id)
    node_found = perf_counter()
    try:
        # 这两个内部 GET 均无发送命令等副作用。响应尚未完整返回时，按同一
        # 文件偏移或实时游标重试一次，不推进游标；超时与连接池排队不重试。
        for attempt in range(2):
            try:
                response = await client.get(node["url"]+path, params=params,
                    headers={"Authorization": "Bearer "+repo.settings.internal_token})
                break
            except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
                if attempt:
                    raise
                logger.warning("节点只读连接中断，按原游标重试一次 node=%s path=%s error=%s",
                               node_id, path, type(exc).__name__)
        if response.status_code != 200:
            raise HTTPException(503, "采集节点文件暂不可用")
        received = perf_counter()
        body = response.json()
        if downstream is not None:
            # 上游耗时包含节点内部阶段，不能与节点阶段再次相加。
            api_timing = (f"api_node;dur={(node_found-started)*1000:.3f}, "
                          f"api_upstream;dur={(received-node_found)*1000:.3f}, "
                          f"api_decode;dur={(perf_counter()-received)*1000:.3f}")
            downstream.headers["Server-Timing"] = ", ".join(part for part in (
                downstream.headers.get("Server-Timing"), response.headers.get("server-timing"), api_timing) if part)
        return body
    except httpx.HTTPError as exc:
        logger.warning("节点 JSON 转发失败 node=%s path=%s error=%s", node_id, path, type(exc).__name__)
        raise HTTPException(503, "采集节点暂不可用") from exc


async def proxy_file(repo, node_id, path, range_header=None):
    """流式代理文件及 Range 响应；客户端离开后释放上游连接，不整包驻留内存。"""
    node = await repo.get("nodes", node_id)
    client = httpx.AsyncClient(timeout=120)
    headers = {"Authorization": "Bearer "+repo.settings.internal_token}
    if range_header:
        headers["Range"] = range_header
    try:
        response = await client.send(client.build_request("GET", node["url"]+path, headers=headers), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(503, "下载节点暂不可用") from exc
    if response.status_code not in (200, 206):
        status = response.status_code
        await response.aclose()
        await client.aclose()
        raise HTTPException(status if status == 416 else 503, "下载文件不可用")
    async def chunks():
        try:
            async for chunk in response.aiter_bytes(262144):
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()
    forwarded = {key: value for key, value in response.headers.items()
                 if key in {"content-length", "content-range", "accept-ranges", "content-disposition", "content-type"}}
    return StreamingResponse(chunks(), status_code=response.status_code, headers=forwarded)


def install_log_routes(app):
    """注册共用正式 API 的目录、作业、下载及 WebSocket 接口。"""
    User = Annotated[dict, Depends(actor)]
    def repo():
        return app.state.repo

    @app.get("/api/v1/tasks/{task_id}/log-hours")
    async def hours(task_id: str, user: User, page: int = Query(1, ge=1), pageSize: int = Query(100, ge=1, le=168),
                    date: Annotated[CalendarDate | None, Query()] = None):
        authorize(user, "logs:read", task_id)
        await repo().get("tasks", task_id)
        query = {"taskId": task_id, "status": {"$ne": "DELETED"}}
        if date is not None:
            start = datetime.combine(date, datetime.min.time(), ZoneInfo("Asia/Shanghai"))
            query["hour"] = {"$gte": start.astimezone(now().tzinfo).isoformat(),
                             "$lt": (start+timedelta(days=1)).astimezone(now().tzinfo).isoformat()}
        values = summarize_hours([file async for file in repo().db.files.find(query)])
        return {"items": values[(page-1)*pageSize:page*pageSize], "total": len(values), "page": page, "pageSize": pageSize}

    @app.get("/api/v1/log-files/{identifier}/content")
    async def content(identifier: str, request: Request, response: Response, user: User, offset: int = Query(0, ge=0), limit: int = Query(65536, ge=1, le=262144)):
        started = perf_counter()
        file = await repo().get("files", identifier)
        authorize(user, "logs:read", file["taskId"])
        response.headers["Server-Timing"] = f"api_catalog;dur={(perf_counter()-started)*1000:.3f}"
        return await node_request(repo(), file["nodeId"], f"/internal/read/{identifier}",
                                  {"offset": offset, "limit": limit}, client=request.app.state.node_http,
                                  downstream=response)

    async def create_job(body, request, user, kind):
        """冻结任务文件目录，申请保留期保护，并通过幂等键提交后台作业。"""
        authorize(user, "logs:download" if kind == "DOWNLOAD" else "logs:read", body.taskId)
        task = await repo().get("tasks", body.taskId)
        async def build(identifier):
            query = {"taskId": body.taskId, "status": {"$ne": "DELETED"}}
            if kind == "DOWNLOAD":
                query["hour"] = {"$in": body.hourIds}
            else:
                try:
                    end = datetime.fromisoformat(body.end) if body.end else now()
                    start = datetime.fromisoformat(body.start) if body.start else end-timedelta(hours=1)
                    if start.tzinfo is None or end.tzinfo is None or not timedelta(0) < end-start <= timedelta(hours=24):
                        raise ValueError()
                except ValueError as exc:
                    raise HTTPException(422, "时间范围须包含时区且不超过24小时") from exc
                start, end = start.astimezone(now().tzinfo), end.astimezone(now().tzinfo)
                query["hour"] = {"$gte": start.replace(minute=0, second=0, microsecond=0).isoformat(), "$lte": end.isoformat()}
            files = ordered_files([public(f) async for f in repo().db.files.find(query)])
            if not files:
                raise HTTPException(404, "所选范围没有日志")
            unavailable = [f for f in files if f["status"] not in ("OPEN", "READY")]
            if unavailable and (kind != "DOWNLOAD" or not body.allowPartial):
                raise HTTPException(409, "所选范围包含暂不可用片段，请刷新或明确允许部分导出")
            # 发布作业前保护所有可用片段；不可用片段仍保留在清单中，由作业明确报告。
            for file in files:
                if file["status"] not in ("OPEN", "READY"):
                    continue
                claim = await repo().db.files.update_one({"id": file["id"], "status": {"$in": ["OPEN", "READY"]}},
                    {"$max": {"retainUntil": now()+timedelta(hours=24)}})
                if claim.matched_count != 1:
                    raise HTTPException(409, "日志片段正在清理，请刷新后重试")
            if kind == "DOWNLOAD" and not body.allowPartial and set(body.hourIds)-{f["hour"] for f in files}:
                raise HTTPException(409, "部分小时没有可用片段")
            archive_sizes = {}
            for file in files:
                group = (file["nodeId"], file.get("archiveGroupId") or file["id"])
                archive_sizes[group] = max(archive_sizes.get(group, 0), file.get("archiveBytes") or file.get("bytes", 0))
            estimate = sum(archive_sizes.values())
            if kind == "DOWNLOAD" and estimate > 20_000_000_000:
                raise HTTPException(422, "预计下载超过20GB，请拆分小时")
            doc = body.model_dump() | {"id": identifier, "kind": kind, "status": "QUEUED", "progress": 0,
                "actor": user["id"], "files": files, "nodeId": task.get("nodeId") or files[0]["nodeId"],
                "taskName": task["name"], "taskIp": task["ip"],
                "createdAt": now(), "expiresAt": now()+timedelta(hours=24)}
            if kind == "SEARCH":
                doc.update(start=start.isoformat(), end=end.isoformat())
            await repo().db.jobs.insert_one(doc)
            return doc
        return public(await repo().idem(user["id"], request.headers.get("Idempotency-Key"), kind,
                                        body.model_dump(), "jobs", build))

    @app.post("/api/v1/downloads", status_code=202)
    async def download(body: DownloadCreate, request: Request, user: User):
        return await create_job(body, request, user, "DOWNLOAD")

    @app.post("/api/v1/log-searches", status_code=202)
    async def search(body: SearchCreate, request: Request, user: User):
        return await create_job(body, request, user, "SEARCH")

    @app.get("/api/v1/downloads/{identifier}")
    @app.get("/api/v1/log-searches/{identifier}")
    async def job(identifier: str, user: User):
        doc = await repo().get("jobs", identifier)
        authorize(user, "logs:download" if doc["kind"] == "DOWNLOAD" else "logs:read", doc["taskId"])
        return public(doc)

    @app.get("/api/v1/log-searches/{identifier}/results")
    async def results(identifier: str, user: User, page: int = Query(1, ge=1), pageSize: int = Query(100, ge=1, le=100)):
        doc = await repo().get("jobs", identifier)
        authorize(user, "logs:read", doc["taskId"])
        items = doc.get("results", [])
        return {"items": items[(page-1)*pageSize:page*pageSize], "total": len(items), "page": page, "pageSize": pageSize,
                "status": doc["status"], "truncated": doc.get("truncated", False)}

    @app.get("/api/v1/downloads/{identifier}/content")
    async def download_content(identifier: str, request: Request, user: Annotated[dict, Depends(download_actor)]):
        doc = await repo().get("jobs", identifier)
        authorize(user, "logs:download", doc["taskId"])
        if doc["status"] != "SUCCEEDED":
            raise HTTPException(409, "导出尚未完成")
        await repo().audit(user["id"], "download_content", identifier)
        return await proxy_file(repo(), doc["nodeId"], f"/internal/downloads/{identifier}", request.headers.get("range"))

    @app.delete("/api/v1/downloads/{identifier}", status_code=204)
    @app.delete("/api/v1/log-searches/{identifier}", status_code=204)
    async def cancel(identifier: str, user: User):
        doc = await repo().get("jobs", identifier)
        authorize(user, "logs:read", doc["taskId"])
        if doc["actor"] != user["id"]:
            authorize(user, "admin")
        changed = await repo().db.jobs.update_one(
            {"id": identifier, "status": {"$in": ["QUEUED", "RUNNING"]}},
            {"$set": {"status": "CANCELLED"}},
        )
        # 只有确实改变作业状态才记录取消，避免已结束作业产生错误审计事实。
        if changed.modified_count:
            await repo().audit(user["id"], f"cancel_{doc['kind'].lower()}", identifier)

    @app.websocket("/api/v1/tasks/{task_id}/logs")
    async def live(ws: WebSocket, task_id: str):
        """首帧鉴权后按游标推送字节块；发送超时关闭慢连接，正文可通过文件接口补读。"""
        await ws.accept()
        try:
            hello = await asyncio.wait_for(ws.receive_json(), timeout=10)
            async def current_identity():
                """每次推送重验会话与范围；权限撤销不能留下旧实时订阅。"""
                if hello.get("token"):
                    return await apply_ip_permissions(repo(), ws, await authenticate(repo(), hello["token"]))
                check_origin(ws)
                identity = await session_identity(repo(), ws.cookies.get(COOKIE, ""))
                if identity.get("mustChangePassword"):
                    raise HTTPException(403, "请先修改初始密码")
                return await apply_ip_permissions(repo(), ws, identity)

            user = await current_identity()
            # 中间件只读取已鉴权的主体标识，绝不保留客户端首帧中的原始令牌。
            ws.state.actor = user
            authorize(user, "logs:read", task_id)
            await repo().audit(user["id"], "live_subscribe", task_id)
            cursor = hello.get("cursor")
            while True:
                user = await current_identity()
                authorize(user, "logs:read", task_id)
                task = await repo().get("tasks", task_id)
                if not task.get("nodeId"):
                    await ws.send_json({"type": "status", "status": task["status"]})
                else:
                    data = await node_request(repo(), task["nodeId"], f"/internal/tail/{task_id}",
                                              {"cursor": cursor or ""}, client=ws.app.state.node_http)
                    if data.get("gap"):
                        await ws.send_json({"type": "gap", "message": "实时缓冲已过期，请通过小时归档补读"})
                    for frame in data["frames"]:
                        await asyncio.wait_for(ws.send_json(frame), timeout=5)
                        cursor = frame["cursor"]
                await asyncio.sleep(.1)
        except TimeoutError:
            ws.state.failure_type = "TimeoutError"
            await ws.close(code=4408, reason="订阅交互超时")
        except WebSocketDisconnect:
            # 正常断开由 WebSocket 中间件依据 closeCode 记录，不重复写错误事件。
            pass
        except HTTPException as exc:
            ws.state.failure_type = "HTTPException"
            code = {401: 4401, 403: 4403}.get(exc.status_code, 1013)
            await ws.close(code=code, reason=str(exc.detail)[:120])
        except Exception as exc:
            ws.state.failure_type = type(exc).__name__
            import logging
            logging.getLogger(__name__).exception("实时订阅异常 task=%s", task_id)
            try:
                await ws.close(code=1011)
            except RuntimeError:
                pass
