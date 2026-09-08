"""节点内部文件接口：受鉴权的字节读取、快照和 Range 下载。

文件必须属于当前节点且解析路径不能逃逸日志根目录；对外访问由正式 API
代理并校验任务权限。快照响应完成后清理临时文件，保持当前小时采集不受影响。
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import tarfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from camera_logs.logs.archive_access import LimitedReader, read_limiter, snapshot


def _root(runtime: Any, repo: Any) -> Path:
    return Path(getattr(runtime, "log_root", repo.settings.log_root)).resolve()


def _path(runtime: Any, repo: Any, file: dict[str, Any]) -> Path:
    """校验目录中登记的本地路径，拒绝目录穿越和已被删除的文件。"""
    raw = file.get("path") or file.get("rawPath")
    if not raw:
        raise HTTPException(404, "日志文件不存在")
    path = Path(raw)
    path = path.resolve() if path.is_absolute() else (_root(runtime, repo) / path).resolve()
    if path != _root(runtime, repo) and _root(runtime, repo) not in path.parents:
        raise HTTPException(403, "日志文件路径非法")
    if not path.is_file():
        raise HTTPException(404, "日志文件不存在")
    return path


def install_node_routes(app: Any, repo: Any, runtime: Any) -> None:
    """注册仅供节点和 API 使用的内部路由，每次调用都校验内部令牌。"""
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
        file = await get_file(identifier)
        path = _path(runtime, repo, file)
        def load() -> bytes:
            cap = int(file.get("bytes", path.stat().st_size))
            if path.suffix == ".log":
                with path.open("rb") as handle:
                    handle.seek(offset)
                    data = handle.read(min(limit, max(0, cap - offset)))
                    read_limiter.consume(len(data)); return data
            with tarfile.open(path, "r:gz") as archive:
                member = next((item for item in archive if item.name.endswith(".log")), None)
                if member is None: raise FileNotFoundError(path)
                stream = LimitedReader(archive.extractfile(member), min(cap or member.size, member.size))
                remaining = offset
                while remaining:
                    skipped = stream.read(min(262144, remaining))
                    if not skipped: return b""
                    remaining -= len(skipped)
                return stream.read(limit)
        data = await asyncio.to_thread(load)
        return {"fileId": identifier, "sessionId": file.get("sessionId"), "data": base64.b64encode(data).decode(), "nextOffset": offset + len(data)}

    @app.get("/internal/archive/{identifier}")
    async def archive(identifier: str, _: None = Depends(internal), bytes: int | None = Query(default=None, ge=0)):
        file = await get_file(identifier)
        path = _path(runtime, repo, file)
        if bytes is None and file.get("status") == "READY" and path.suffixes[-2:] == [".tar", ".gz"]:
            return FileResponse(path, media_type="application/gzip", filename=path.name)
        snapshot_path = _root(runtime, repo) / "exports" / ".snapshots" / f"{identifier}-{uuid.uuid4().hex}.tar.gz"
        index = _path(runtime, repo, {"path": file["indexPath"]}) if file.get("indexPath") else None
        await asyncio.to_thread(snapshot, path, snapshot_path, bytes if bytes is not None else file.get("bytes", path.stat().st_size), index, identifier)
        return FileResponse(snapshot_path, media_type="application/gzip", filename=f"{identifier}.tar.gz", background=BackgroundTask(snapshot_path.unlink, missing_ok=True))

    @app.get("/internal/downloads/{identifier}")
    async def download(identifier: str, _: None = Depends(internal)):
        job = await repo.db.jobs.find_one({"id": identifier, "status": "SUCCEEDED"})
        expires = job.get("expiresAt") if job else None
        if not job or not job.get("resultPath") or (expires and expires.astimezone(UTC) <= datetime.now(UTC)):
            raise HTTPException(404, "导出文件不存在")
        path = _path(runtime, repo, {"path": job["resultPath"]})
        return FileResponse(path, filename=job.get("filename") or path.name)
