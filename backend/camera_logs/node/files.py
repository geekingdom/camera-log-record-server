"""节点内部文件接口：受鉴权的字节读取、快照和 Range 下载。

文件必须属于当前节点且解析路径不能逃逸日志根目录；对外访问由正式 API
代理并校验任务权限。快照响应完成后清理临时文件，保持当前小时采集不受影响。
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import os
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import quote

from fastapi import Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from camera_logs.logs.archive_access import read_limiter, snapshot
from camera_logs.logs.file_reads import FileReads
from camera_logs.logs.job_threads import job_thread


def _root(runtime: Any, repo: Any) -> Path:
    return Path(getattr(runtime, "log_root", repo.settings.log_root)).resolve()


def _path(runtime: Any, repo: Any, file: dict[str, Any], *, require_exists: bool = True) -> Path:
    """校验目录中登记的本地路径，拒绝目录穿越和已被删除的文件。"""
    raw = file.get("path") or file.get("rawPath")
    if not raw:
        raise HTTPException(404, "日志文件不存在")
    path = Path(raw)
    path = path.resolve() if path.is_absolute() else (_root(runtime, repo) / path).resolve()
    if path != _root(runtime, repo) and _root(runtime, repo) not in path.parents:
        raise HTTPException(403, "日志文件路径非法")
    if require_exists and not path.is_file():
        raise HTTPException(404, "日志文件不存在")
    return path


def _read_watermark(runtime: Any, file: dict[str, Any]) -> int | None:
    """当前会话可读取已确认写入的尾部；历史文件只使用登记水位，不按磁盘大小推断成功。"""
    registered = file.get("bytes")
    identity = (file.get("taskId"), file.get("runId"), file.get("sessionId"))
    if file.get("status") != "OPEN" or not all(identity):
        return registered
    session = getattr(runtime, "active", {}).get(identity[0])
    if session is None or getattr(session, "retired", False):
        return registered
    collector = session.collector
    if (session.task.get("id"), session.task.get("runId"), getattr(collector, "session_id", None)) != identity:
        return registered
    confirmed = session.paths.get(file["id"])
    return max(registered or 0, confirmed) if confirmed is not None else registered


async def _limited_response(
    reads: FileReads,
    downloads: Any,
    path: Path,
    request: Request,
    *,
    filename: str,
    etag: str | None = None,
    on_close: Callable[[], Awaitable[None]] | None = None,
    before_read: Callable[[], None] | None = None,
):
    """以有界读取池和全局读预算流式提供固定文件，断流 finally 必定关闭 fd。"""
    try:
        size = (await reads.run(path.stat)).st_size
    except FileNotFoundError as error:
        raise HTTPException(404, "冻结副本已过期") from error
    start, end = 0, size - 1
    range_header = request.headers.get("range", "")
    if request.headers.get("if-range") not in (None, etag):
        range_header = ""
    if range_header:
        try:
            unit, value = range_header.split("=", 1)
            left, right = value.split("-", 1)
            if unit != "bytes" or "," in value:
                raise ValueError
            if not left and not right:
                raise ValueError
            if left:
                start = int(left)
                end = int(right) if right else size - 1
            else:
                suffix = int(right)
                if suffix <= 0:
                    raise ValueError
                start, end = max(0, size - suffix), size - 1
            if start < 0 or end < start or start >= size:
                raise ValueError
            end = min(end, size - 1)
        except (ValueError, OverflowError):
            raise HTTPException(416, "Range 不可用", headers={"Content-Range": f"bytes */{size}"}) from None
    remaining = end - start + 1
    await downloads.acquire()
    descriptor = None
    try:
        descriptor = await reads.run(os.open, path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        await reads.run(os.lseek, descriptor, start, os.SEEK_SET)
    except BaseException:
        if descriptor is not None:
            await reads.run(os.close, descriptor)
        downloads.release()
        raise
    released = False

    async def close_descriptor() -> None:
        """响应尚未进入正文或已进入正文时都只释放 fd、下载槽和外部租约一次。"""
        nonlocal released
        if not released:
            released = True
            try:
                try:
                    await reads.run(os.close, descriptor)
                finally:
                    downloads.release()
            finally:
                if on_close is not None:
                    await on_close()
    def read_chunk():
        data = os.read(descriptor, min(262144, remaining))
        read_limiter.consume(len(data))
        return data
    async def chunks():
        nonlocal remaining
        try:
            while remaining:
                if before_read is not None:
                    before_read()
                data = await reads.run(read_chunk)
                if not data:
                    raise RuntimeError("冻结副本长度变化")
                remaining -= len(data)
                yield data
        finally:
            await close_descriptor()
    safe_name = Path(filename).name.replace("\r", "").replace("\n", "") or "download"
    headers = {"Accept-Ranges": "bytes", "Content-Disposition": f"attachment; filename=download; filename*=UTF-8''{quote(safe_name)}",
               "Cache-Control": "private, no-store", "Content-Length": str(end - start + 1)}
    if etag:
        headers["ETag"] = etag
    if range_header:
        headers["Content-Range"] = f"bytes {start}-{end}/{size}"
    class LimitedStreamingResponse(StreamingResponse):
        """即使 ASGI 在 response.start 前失败，也要收回预先取得的描述符。"""
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                await close_descriptor()

    return LimitedStreamingResponse(chunks(), status_code=206 if range_header else 200,
                                    media_type="application/octet-stream", headers=headers)


def install_node_routes(app: Any, repo: Any, runtime: Any) -> FileReads:
    """注册仅供节点和 API 使用的内部路由，每次调用都校验内部令牌。"""
    reads = FileReads()
    # 活跃下载而非瞬时 read() 受限，确保慢客户端也不会耗尽文件描述符。
    downloads = asyncio.Semaphore(4)
    # 独立测试应用使用默认 ASGI 生命周期；正式 Worker 在自己的 lifespan 中显式关闭。
    app.router.add_event_handler("shutdown", reads.close)
    async def internal(authorization: str | None = Header(default=None)) -> None:
        expected = "Bearer " + repo.settings.internal_token
        if not repo.settings.internal_token or not authorization or not hmac.compare_digest(authorization, expected):
            raise HTTPException(401, "内部节点认证失败")

    async def get_file(identifier: str) -> dict[str, Any]:
        file = await repo.db.files.find_one({"id": identifier})
        if not file or (file.get("nodeId") and file["nodeId"] != repo.settings.node_id):
            raise HTTPException(404, "日志文件不存在")
        return file

    @app.get("/internal/read/{identifier}")
    async def read(identifier: str, _: None = Depends(internal), offset: int = Query(0, ge=0), limit: int = Query(65536, ge=1, le=262144)):
        started = perf_counter()
        file = await get_file(identifier)
        catalog_done = perf_counter()
        path = _path(runtime, repo, file, require_exists=False)
        # 在事件循环中固定本次读取边界，工作线程不再访问可变化的会话映射。
        watermark = _read_watermark(runtime, file) if path.suffix == ".log" else file.get("bytes")
        timings = {"catalog": (catalog_done - started) * 1000,
                   "path": (perf_counter() - catalog_done) * 1000}
        try:
            identity = tuple(file.get(key) for key in ("id", "taskId", "runId", "sessionId", "nodeId"))
            data = await reads.read(identity, path, file.get("archiveMember"), offset, limit, watermark,
                                    timings=timings)
        except FileNotFoundError as error:
            raise HTTPException(404, "归档成员不存在") from error
        encoding = perf_counter()
        response = JSONResponse({"fileId": identifier, "sessionId": file.get("sessionId"),
                                 "data": base64.b64encode(data).decode(), "nextOffset": offset + len(data)})
        timings["encode"] = (perf_counter() - encoding) * 1000
        # 只返回固定阶段名和毫秒值；不记录正文、路径、凭据或用户输入。
        response.headers["Server-Timing"] = ", ".join(f"{key};dur={value:.3f}" for key, value in timings.items())
        return response

    @app.get("/internal/archive/{identifier}")
    async def archive(identifier: str, _: None = Depends(internal), bytes: int | None = Query(default=None, ge=0), includeIndex: bool = Query(default=False)):
        file = await get_file(identifier)
        path = _path(runtime, repo, file, require_exists=False)
        if bytes is None and not includeIndex and file.get("status") == "READY" and path.suffixes[-2:] == [".tar", ".gz"]:
            path = _path(runtime, repo, file)
            return FileResponse(path, media_type="application/gzip", filename=path.name)
        snapshot_path = _root(runtime, repo) / "exports" / ".snapshots" / f"{identifier}-{uuid.uuid4().hex}.tar.gz"
        index = _path(runtime, repo, {"path": file["indexPath"]}) if includeIndex and file.get("indexPath") else None
        frozen_bytes = bytes if bytes is not None else file.get("bytes")
        if frozen_bytes is None:
            raise HTTPException(409, "日志文件缺少已确认字节水位")
        try:
            await reads.run(snapshot, path, snapshot_path, frozen_bytes, index, identifier, file.get("archiveMember"), includeIndex)
        except BaseException as error:
            # reads.run 已等待文件线程退出，取消或失败后可以安全回收未发布快照。
            await job_thread(snapshot_path.unlink, missing_ok=True)
            if isinstance(error, FileNotFoundError):
                raise HTTPException(404, "归档成员不存在") from error
            raise
        return FileResponse(snapshot_path, media_type="application/gzip", filename=f"{identifier}.tar.gz", background=BackgroundTask(snapshot_path.unlink, missing_ok=True))

    @app.get("/internal/downloads/{identifier}")
    async def download(identifier: str, _: None = Depends(internal)):
        job = await repo.db.jobs.find_one({"id": identifier, "status": "SUCCEEDED"})
        expires = job.get("expiresAt") if job else None
        if not job or not job.get("resultPath") or (expires and expires.astimezone(UTC) <= datetime.now(UTC)):
            raise HTTPException(404, "导出文件不存在")
        path = _path(runtime, repo, {"path": job["resultPath"]})
        return FileResponse(path, filename=job.get("filename") or path.name)

    @app.post("/internal/coredumps/{identifier}/freeze")
    async def freeze_coredump(identifier: str, _: None = Depends(internal)):
        """节点按当前登记版本创建不可变副本；源文件从不直接暴露下载。"""
        from camera_logs.coredumps.snapshots import freeze

        document = await repo.db.coredump_files.find_one({"id": identifier, "nodeId": repo.settings.node_id})
        if document is None:
            raise HTTPException(404, "coredump 不存在")
        try:
            result = await freeze(repo, document)
        except OverflowError as error:
            raise HTTPException(413, str(error)) from error
        except (FileNotFoundError, RuntimeError, ValueError) as error:
            raise HTTPException(409, "coredump 源文件仍在变化或不可安全冻结，请稍后重试") from error
        return {"id": result["id"], "status": result["status"], "size": result["snapshot"]["size"],
                "etag": result["snapshot"]["etag"]}

    @app.get("/internal/coredumps/{identifier}/content")
    async def coredump_content(
        identifier: str,
        request: Request,
        _: None = Depends(internal),
        parent_reader: str | None = Header(default=None, alias="X-Coredump-Reader"),
    ):
        """只响应节点已发布的固定副本；If-Range 不匹配时返回完整固定版本。"""
        from camera_logs.coredumps.snapshot_readers import SnapshotReader

        document = await repo.db.coredump_files.find_one({"id": identifier, "nodeId": repo.settings.node_id})
        snapshot = document.get("snapshot") if document else None
        retiring_with_parent = document and document.get("status") == "RETIRING" and parent_reader
        if not snapshot or (document.get("status") != "FROZEN" and not retiring_with_parent):
            raise HTTPException(409, "coredump 尚未冻结")
        reader = SnapshotReader(repo, document, parent_id=parent_reader)
        try:
            await reader.acquire()
            path = Path(snapshot["path"])
            root = Path(repo.settings.log_root).resolve().parent / "coredump-snapshots"
            if root not in path.resolve().parents or not path.is_file():
                raise ValueError()
            return await _limited_response(
                reads,
                downloads,
                path,
                request,
                filename=document["name"],
                etag=snapshot["etag"],
                on_close=reader.close,
                before_read=reader.assert_active,
            )
        except (FileNotFoundError, RuntimeError, ValueError):
            await reader.close()
            raise HTTPException(404, "coredump 快照已过期") from None
        except BaseException:
            await reader.close()
            raise

    @app.get("/internal/coredump-exports/{identifier}/content")
    async def coredump_export_content(identifier: str, request: Request, _: None = Depends(internal)):
        """导出产物仅在成功和未过期时由协调节点流式提供。"""
        job = await repo.db.coredump_exports.find_one({"id": identifier, "status": "SUCCEEDED"})
        expires = job.get("expiresAt") if job else None
        if not job or not job.get("resultPath") or job.get("coordinatorNodeId") != repo.settings.node_id or (expires and expires.astimezone(UTC) <= datetime.now(UTC)):
            raise HTTPException(404, "coredump 导出不存在")
        path = Path(job["resultPath"])
        root = Path(repo.settings.log_root).resolve() / "exports" / "coredumps"
        if root not in path.resolve().parents or not path.is_file():
            raise HTTPException(404, "coredump 导出已过期")
        return await _limited_response(reads, downloads, path, request, filename=job.get("filename") or path.name,
                                       etag=job.get("etag"))

    return reads
