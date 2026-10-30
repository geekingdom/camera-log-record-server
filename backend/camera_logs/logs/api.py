"""日志目录、字节读取、搜索导出作业和可恢复的实时订阅接口。

正文保留在采集节点，由此层鉴权后转发；导出冻结文件清单与字节水位，避免
查询执行期间的小时轮转改变结果。实时推送允许显式缺口，但不能影响归档保存。
"""
import asyncio
import logging
from datetime import UTC, datetime, timedelta
from datetime import date as CalendarDate
from hashlib import sha256
from time import perf_counter
from typing import Annotated
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi import Path as ApiPath
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from camera_logs.access_policy.policy import apply_ip_permissions
from camera_logs.common.log_frames import GapFrame, StatusFrame, SubscribeFrame
from camera_logs.common.models import DownloadCreate, SearchCreate
from camera_logs.common.presentation_time import utc_iso
from camera_logs.common.security import actor, authenticate, authorize
from camera_logs.common.websocket_logging import bind_websocket_actor
from camera_logs.logs.download_availability import download_available
from camera_logs.logs.gap_catalog import catalog_cursor_page, catalog_gap_fragments
from camera_logs.logs.hour_catalog import summarize_hours
from camera_logs.logs.job_submission import cancel_job, submit_job
from camera_logs.logs.live_subscription import await_live_operation, watch_disconnect
from camera_logs.logs.public import public_job
from camera_logs.users.sessions import COOKIE, check_origin, session_identity

logger = logging.getLogger(__name__)
SHANGHAI = ZoneInfo("Asia/Shanghai")


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


async def proxy_file(repo, node_id, path, range_header=None, if_range=None):
    """流式代理文件及 Range 响应；客户端离开后释放上游连接，不整包驻留内存。"""
    node = await repo.get("nodes", node_id)
    client = httpx.AsyncClient(timeout=120)
    headers = {"Authorization": "Bearer "+repo.settings.internal_token}
    if range_header:
        headers["Range"] = range_header
    if if_range:
        headers["If-Range"] = if_range
    try:
        response = await client.send(client.build_request("GET", node["url"]+path, headers=headers), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(503, "下载节点暂不可用") from exc
    if response.status_code == 416:
        content_range = response.headers.get("content-range")
        await response.aclose()
        await client.aclose()
        raise HTTPException(416, "下载范围不可用", headers={"Content-Range": content_range} if content_range else None)
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
                 if key in {"content-length", "content-range", "accept-ranges", "content-disposition", "content-type", "etag", "cache-control"}}
    class ProxiedStreamingResponse(StreamingResponse):
        """上游已在响应正文前建立，任意 ASGI 失败均需主动归还连接。"""
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                await response.aclose()
                await client.aclose()
    return ProxiedStreamingResponse(chunks(), status_code=response.status_code, headers=forwarded)


def install_log_routes(app):
    """注册共用正式 API 的目录、作业、下载及 WebSocket 接口。"""
    User = Annotated[dict, Depends(actor)]
    def repo():
        return app.state.repo

    @app.get("/api/v1/tasks/{task_id}/log-hours")
    async def hours(task_id: Annotated[str, ApiPath(description='路径中指定要查询上海小时归档目录的日志采集任务标识。')], user: User, page: int = Query(1, ge=1, description='从 1 开始的分页页码。'), pageSize: int = Query(100, ge=1, le=168, description='每页返回的记录数，受接口允许的最大值限制。'),
                    date: Annotated[CalendarDate | None, Query(description='按 Asia/Shanghai 自然日筛选任务小时归档或作为看板统计截至日。')] = None):
        authorize(user, "logs:read", task_id)
        await repo().get("tasks", task_id)
        query = {"taskId": task_id, "status": {"$ne": "DELETED"}}
        if date is not None:
            start = datetime.combine(date, datetime.min.time(), ZoneInfo("Asia/Shanghai"))
            # hour 是历史 UTC 文本键，查询不能受展示时区或 now() 返回时区影响。
            query["hour"] = {"$gte": utc_iso(start), "$lt": utc_iso(start + timedelta(days=1))}
        values = summarize_hours([file async for file in repo().db.files.find(query)])
        return {"items": values[(page-1)*pageSize:page*pageSize], "total": len(values), "page": page, "pageSize": pageSize}

    @app.get("/api/v1/tasks/{task_id}/log-days")
    async def days(task_id: Annotated[str, ApiPath(description='路径中指定要查询可用日志日期的日志采集任务标识。')], user: User,
                   month: Annotated[str, Query(pattern=r"^\d{4}-\d{2}$", description="上海时区月份，格式 YYYY-MM")]):
        """返回指定上海月份内存在有效非空日志的自然日，不读取日志正文或片段元数据。"""
        authorize(user, "logs:read", task_id)
        await repo().get("tasks", task_id)
        try:
            start = datetime.strptime(month, "%Y-%m").replace(tzinfo=SHANGHAI)
            end = start.replace(year=start.year + (start.month == 12),
                                month=1 if start.month == 12 else start.month + 1)
            lower, upper = utc_iso(start), utc_iso(end)
        except (OverflowError, ValueError) as exc:
            raise HTTPException(422, "month必须是有效的 YYYY-MM 月份") from exc
        query = {
            "taskId": task_id,
            "hour": {"$gte": lower, "$lt": upper},
            "status": {"$in": ["OPEN", "READY"]},
            "bytes": {"$gt": 0},
        }
        hours = await repo().db.files.distinct("hour", query)
        return {"days": sorted({datetime.fromisoformat(str(hour)).astimezone(SHANGHAI).date().isoformat()
                                for hour in hours})}

    @app.get("/api/v1/log-files/{identifier}/content")
    async def content(identifier: Annotated[str, ApiPath(description='路径中指定要读取内容的日志分卷目录标识。')], request: Request, response: Response, user: User, offset: int = Query(0, ge=0, description='从日志文件开头开始读取的字节偏移量。'), limit: int = Query(65536, ge=1, le=262144, description='单次读取日志内容的最大字节数。')):
        started = perf_counter()
        file = await repo().get("files", identifier)
        authorize(user, "logs:read", file["taskId"])
        response.headers["Server-Timing"] = f"api_catalog;dur={(perf_counter()-started)*1000:.3f}"
        return await node_request(repo(), file["nodeId"], f"/internal/read/{identifier}",
                                  {"offset": offset, "limit": limit}, client=request.app.state.node_http,
                                  downstream=response)

    @app.get("/api/v1/tasks/{task_id}/log-gap-catalog")
    async def gap_catalog(task_id: Annotated[str, ApiPath(description='路径中指定要查询实时日志缺口目录的日志采集任务标识。')], user: User, beforeFileId: str = Query(min_length=1, description='缺口前最后一个可靠帧所在的日志文件标识。'), beforeOffset: int = Query(ge=0, description='缺口前可靠帧的结束字节偏移，也是待补读的起点。'),
                          beforeSessionId: str = Query(min_length=1, description='缺口前可靠帧所属采集会话标识，必须与文件目录一致。'), afterFileId: str = Query(min_length=1, description='缺口后第一个可靠帧所在的日志文件标识。'),
                          afterOffset: int = Query(ge=0, description='缺口后可靠帧的起始字节偏移，不包含该偏移后的已展示内容。'), afterSessionId: str = Query(min_length=1, description='缺口后可靠帧所属采集会话标识，允许与缺口前不同。'),
                          limit: int = Query(50, ge=1, le=100, description="本页最多返回的文件字节范围数，最大100"),
                          cursor: str | None = Query(None, max_length=32, description="上页返回的短游标，有效期一小时；保持原前后锚点，失效后重新定位")):
        """定位实时 gap 两端之间的已保存片段，文件正文仍由逐文件读取接口提供。"""
        from camera_logs.logs.gap_snapshots import publish_gap_page, resolve_gap_cursor
        authorize(user, "logs:read", task_id)
        await repo().get("tasks", task_id)
        cursor = await resolve_gap_cursor(repo(), task_id, cursor)
        anchors = {"before": {"fileId": beforeFileId, "offset": beforeOffset, "sessionId": beforeSessionId},
                   "after": {"fileId": afterFileId, "offset": afterOffset, "sessionId": afterSessionId}}
        signing_key = sha256((repo().settings.encryption_key + "log-gap-catalog").encode()).digest()
        saved_page = catalog_cursor_page(cursor, anchors, limit, signing_key)
        if saved_page is not None:
            return await publish_gap_page(repo(), task_id, saved_page)
        before = await repo().db.files.find_one({"id": beforeFileId, "taskId": task_id, "status": {"$ne": "DELETED"}})
        after = await repo().db.files.find_one({"id": afterFileId, "taskId": task_id, "status": {"$ne": "DELETED"}})
        if not before or not after:
            return {"items": [], "nextCursor": None, "unrecoverable": [{"reason": "CATALOG_UNAVAILABLE", "message": "缺口锚点文件已不可用。"}]}
        if before.get("sessionId") != beforeSessionId or after.get("sessionId") != afterSessionId:
            return {"items": [], "nextCursor": None, "unrecoverable": [{"reason": "SESSION_MISMATCH", "message": "缺口锚点的会话与文件目录不一致。"}]}
        try:
            first_hour = datetime.fromisoformat(str(before["hour"])).astimezone(UTC)
            last_hour = datetime.fromisoformat(str(after["hour"])).astimezone(UTC)
        except (KeyError, TypeError, ValueError):
            return {"items": [], "nextCursor": None, "unrecoverable": [{"reason": "CATALOG_UNAVAILABLE", "message": "缺口锚点缺少可排序小时目录。"}]}
        low, high = sorted((first_hour, last_hour))
        if high - low > timedelta(hours=24):
            return {"items": [], "nextCursor": None, "unrecoverable": [{"reason": "CATALOG_TOO_BROAD", "message": "缺口锚点超过 24 小时，无法建立有界目录快照。"}]}
        query = {"taskId": task_id, "hour": {"$gte": utc_iso(low), "$lte": utc_iso(high)}}
        files = [file async for file in repo().db.files.find(query).limit(501)]
        if len(files) > 500:
            return {"items": [], "nextCursor": None, "unrecoverable": [{"reason": "CATALOG_TOO_BROAD", "message": "缺口目录超过 500 个文件，请缩小范围或通过小时归档查看。"}]}
        # 锚点本身必须参加排序，即使旧记录的 hour 格式无法命中范围查询。
        known = {file["id"] for file in files}
        files.extend(file for file in (before, after) if file["id"] not in known)
        page = catalog_gap_fragments(files, before_file_id=beforeFileId, before_offset=beforeOffset,
                                     before_session_id=beforeSessionId, after_file_id=afterFileId,
                                     after_offset=afterOffset, after_session_id=afterSessionId,
                                     limit=limit, cursor=cursor, signing_key=signing_key)
        return await publish_gap_page(repo(), task_id, page)

    @app.get("/api/v1/log-files/{identifier}")
    async def log_file(identifier: Annotated[str, ApiPath(description='路径中指定要读取元数据的日志分卷目录标识。')], user: User):
        """读取定位整行所需的安全文件元数据，禁止泄露节点文件系统路径。"""
        file = await repo().get("files", identifier)
        authorize(user, "logs:read", file["taskId"])
        fields = (
            "id", "taskId", "runId", "sessionId", "nodeId", "status", "bytes", "hour",
            "archiveName", "archiveMember", "rawFileName", "segmentNumber", "firstSequence", "lastSequence",
            "firstReceivedAt", "lastReceivedAt",
        )
        return {name: file[name] for name in fields if name in file}

    async def create_job(body, request, user, kind):
        """冻结任务文件目录，申请保留期保护，并通过幂等键提交后台作业。"""
        authorize(user, "logs:download" if kind == "DOWNLOAD" else "logs:read", body.taskId)
        return public_job(await submit_job(repo(), user, request.headers.get("Idempotency-Key"), body, kind))

    @app.post("/api/v1/downloads", status_code=202, description='创建日志下载作业。taskId必填；hourIds与start/end二选一。时间不带时区时按北京时间解释，开始向下取整至小时、结束向上取整至小时（结束恰为整点不扩展）。例如12:18:41至15:10:08覆盖12:00至16:00的四个小时。最多168小时；默认缺片失败，allowPartial=true才允许部分导出。')
    async def download(body: DownloadCreate, request: Request, user: User):
        return await create_job(body, request, user, "DOWNLOAD")

    @app.post("/api/v1/log-searches", status_code=202, description='创建历史检索作业，冻结指定任务和时间范围内的文件清单后异步扫描。需要logs:read和Idempotency-Key；创建响应仅表示作业已受理，随后查询状态并分页读取结果。')
    async def search(body: SearchCreate, request: Request, user: User):
        return await create_job(body, request, user, "SEARCH")

    @app.get("/api/v1/downloads/{identifier}")
    @app.get("/api/v1/log-searches/{identifier}", description='查询历史检索作业状态。轮询时可传includeResults=false省略较大的结果和冻结文件目录；RUNNING结果只是已扫描部分，只有SUCCEEDED且truncated=false表示冻结范围扫描完整。')
    async def job(identifier: Annotated[str, ApiPath(description='路径中指定日志下载作业或日志检索作业的稳定标识。')], user: User, includeResults: Annotated[bool, Query(description='检索作业状态是否包含完整结果和冻结文件清单，默认true兼容原接口；轮询建议false并从results接口分页读取已确认命中。')] = True):
        """查询作业状态；轮询可省略较大的结果正文与冻结文件目录。"""
        if includeResults:
            doc = await repo().get("jobs", identifier)
        else:
            doc = await repo().db.jobs.find_one({"id": identifier}, {"results": 0, "files": 0})
            if doc is None:
                raise HTTPException(404, "记录不存在")
        authorize(user, "logs:download" if doc["kind"] == "DOWNLOAD" else "logs:read", doc["taskId"])
        return public_job(doc)

    @app.get("/api/v1/tasks/{task_id}/downloads", description='按创建时间倒序分页读取指定任务的日志下载历史，返回作业状态和下载制品当前可用性；不暴露节点路径、写入租约或设备日志正文。')
    async def download_history(task_id: Annotated[str, ApiPath(description='路径中指定要查询下载历史的日志采集任务标识。')], user: User, page: int = Query(1, ge=1, description='从 1 开始的分页页码。'), pageSize: int = Query(20, ge=1, le=100, description='每页返回的记录数，受接口允许的最大值限制。')):
        """按创建时间稳定倒序列出任务下载历史，历史行不暴露节点路径或写入租约。"""
        authorize(user, "logs:download", task_id)
        await repo().get("tasks", task_id)
        query = {"taskId": task_id, "kind": "DOWNLOAD"}
        total = await repo().db.jobs.count_documents(query)
        # 列表不携带作业冻结的数千分卷目录，避免分页行数有限但响应体无限放大。
        fields = {key: 1 for key in ("id", "kind", "taskId", "nodeId", "status", "createdAt", "updatedAt",
            "expiresAt", "artifactCreatedAt", "artifactRetentionDays", "filename", "bytes", "progress", "error",
            "resultPath", "outputExecutionState", "outputCleanupState", "createdBy", "createdByName",
            "actor", "artifactLogSegmentCount")}
        fields["artifactLogSegments"] = {"$slice": 8}
        cursor = repo().db.jobs.find(query, fields).sort([("createdAt", -1), ("id", -1)]).skip((page - 1) * pageSize).limit(pageSize)
        items = []
        async for document in cursor:
            item = public_job(document)
            if not item.get("createdBy") and document.get("actor"):
                item["createdBy"] = document["actor"]
            item.pop("actor", None)  # 历史内部主体已映射为公开创建人字段，避免重复公开。
            item["downloadAvailable"] = download_available(document)
            items.append(item)
        return {"items": items, "total": total, "page": page, "pageSize": pageSize}

    @app.get("/api/v1/log-searches/{identifier}/results", description='分页读取历史检索作业已确认的结果。结果按作业当前状态返回；FAILED、CANCELLED或EXPIRED保留的部分结果不代表剩余范围不存在匹配。')
    async def results(identifier: Annotated[str, ApiPath(description='路径中指定要分页读取结果的日志检索作业标识。')], user: User, page: int = Query(1, ge=1, description='从 1 开始的分页页码。'), pageSize: int = Query(100, ge=1, le=100, description='每页返回的记录数，受接口允许的最大值限制。')):
        doc = await repo().get("jobs", identifier)
        authorize(user, "logs:read", doc["taskId"])
        items = doc.get("results", [])
        return {"items": items[(page-1)*pageSize:page*pageSize], "total": len(items), "page": page, "pageSize": pageSize,
                "status": doc["status"], "truncated": doc.get("truncated", False)}

    @app.get("/api/v1/downloads/{identifier}/content", description='公开下载已生成日志归档，无需登录、Token、票据或来源IP授权。支持Range与If-Range；未完成返回409，已过期或清理返回410。')
    async def download_content(identifier: Annotated[str, ApiPath(description='路径中指定公开下载内容的已完成日志下载作业标识。')], request: Request):
        """已记录下载制品作为缺陷附件公开读取，不依赖用户或短期票据。"""
        doc = await repo().get("jobs", identifier)
        if doc["status"] != "SUCCEEDED":
            raise HTTPException(409, "导出尚未完成")
        if not download_available(doc):
            raise HTTPException(410, "下载制品已过期或已清理")
        await repo().audit("anonymous", "download_content", identifier)
        return await proxy_file(repo(), doc["nodeId"], f"/internal/downloads/{identifier}", request.headers.get("range"), request.headers.get("if-range"))

    @app.delete("/api/v1/downloads/{identifier}", status_code=204)
    @app.delete("/api/v1/log-searches/{identifier}", status_code=204, description='取消本人创建的历史检索作业；取消他人作业需要admin。')
    async def cancel(identifier: Annotated[str, ApiPath(description='路径中指定要取消的日志下载作业或日志检索作业标识。')], user: User):
        doc = await repo().get("jobs", identifier)
        authorize(user, "logs:read", doc["taskId"])
        if doc["actor"] != user["id"]:
            authorize(user, "admin")
        await cancel_job(repo(), user["id"], doc)

    @app.websocket("/api/v1/tasks/{task_id}/logs")
    async def live(ws: WebSocket, task_id: str):
        """首帧鉴权后按游标推送字节块；断开即取消鉴权和节点轮询。"""
        await ws.accept()
        disconnected = None

        async def close_socket(code: int, reason: str | None = None) -> None:
            """客户端已先断开时关闭会抛 RuntimeError；外层取消仍须继续传播。"""
            try:
                await ws.close(code=code, reason=reason)
            except RuntimeError:
                pass

        try:
            try:
                hello = SubscribeFrame.model_validate(await asyncio.wait_for(ws.receive_json(), timeout=10))
            except ValidationError:
                ws.state.failure_type = "ValidationError"
                await close_socket(4408, "订阅首帧格式无效")
                return

            async def current_identity():
                """每次推送重验会话与范围；权限撤销不能留下旧实时订阅。"""
                if hello.token:
                    return await apply_ip_permissions(repo(), ws, await authenticate(repo(), hello.token))
                check_origin(ws)
                identity = await session_identity(repo(), ws.cookies.get(COOKIE, ""), include_service_token=False)
                if identity.get("mustChangePassword"):
                    raise HTTPException(403, "请先修改初始密码")
                return await apply_ip_permissions(repo(), ws, identity)

            # 首帧之后立刻开始接收断开事件，避免后续鉴权或 tail 长轮询成为孤儿任务。
            disconnected = asyncio.create_task(watch_disconnect(ws))
            user = await await_live_operation(current_identity(), disconnected)
            # 中间件只读取已鉴权的主体标识，绝不保留客户端首帧中的原始令牌。
            bind_websocket_actor(ws.scope, user)
            authorize(user, "logs:read", task_id)
            await await_live_operation(repo().audit(user["id"], "live_subscribe", task_id), disconnected)
            cursor = hello.cursor
            while True:
                user = await await_live_operation(current_identity(), disconnected)
                authorize(user, "logs:read", task_id)
                task = await await_live_operation(repo().get("tasks", task_id), disconnected)
                # 删除已被提交后，日志与归档仍可通过 HTTP 查询，但实时订阅必须终止。
                if task.get("deletedAt") is not None:
                    await close_socket(1000, "任务已删除")
                    return
                # 停止已物理完成时不再维持空闲订阅；PAUSED 没有新日志不属于结束。
                if (task.get("desiredState") == "STOPPED" and task.get("status") == "STOPPED"
                        and task.get("nodeId") is None):
                    await close_socket(1000, "任务已停止")
                    return
                if not task.get("nodeId"):
                    await await_live_operation(
                        asyncio.wait_for(
                            ws.send_json(StatusFrame(type="status", status=task["status"]).model_dump(mode="json")),
                            timeout=5,
                        ),
                        disconnected,
                    )
                else:
                    data = await await_live_operation(
                        node_request(repo(), task["nodeId"], f"/internal/tail/{task_id}",
                                     {"cursor": cursor or ""}, client=ws.app.state.node_http),
                        disconnected,
                    )
                    if data.get("gap"):
                        await await_live_operation(asyncio.wait_for(ws.send_json(GapFrame(
                            type="gap", message="实时缓冲已过期，请通过小时归档补读",
                        ).model_dump(mode="json")), timeout=5), disconnected)
                    for frame in data["frames"]:
                        await await_live_operation(asyncio.wait_for(ws.send_json(frame), timeout=5), disconnected)
                        cursor = frame["cursor"]
                await await_live_operation(asyncio.sleep(.1), disconnected)
        except TimeoutError:
            ws.state.failure_type = "TimeoutError"
            await close_socket(4408, "订阅交互超时")
        except WebSocketDisconnect:
            # 正常断开由 WebSocket 中间件依据 closeCode 记录，不重复写错误事件。
            pass
        except HTTPException as exc:
            ws.state.failure_type = "HTTPException"
            code = {401: 4401, 403: 4403}.get(exc.status_code, 1013)
            await close_socket(code, str(exc.detail)[:120])
        except Exception as exc:
            ws.state.failure_type = type(exc).__name__
            import logging
            logging.getLogger(__name__).exception("实时订阅异常 task=%s", task_id)
            await close_socket(1011)
        finally:
            if disconnected is not None:
                disconnected.cancel()
                await asyncio.gather(disconnected, return_exceptions=True)
