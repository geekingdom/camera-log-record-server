"""装配采集节点内部 HTTP 服务、运行监督与进程级资源生命周期。"""

import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from pymongo import AsyncMongoClient

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.common.observability import setup_logging
from camera_logs.node.worker import Worker


def create_worker_app(settings=None):
    """创建节点内部服务；启动失败和正常退出都会按依赖反序释放资源。"""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        listener = client = None
        worker_task = reads = worker = None
        try:
            listener = setup_logging(settings.log_root.parent / "service-logs" / settings.node_id)
            client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                                      w="majority", journal=True)
            repo = Repository(client[settings.database_name], settings)
            await repo.initialize()
            worker = Worker(repo)
            from camera_logs.logs.job_lease import recover_expired_jobs
            await recover_expired_jobs(repo)
            # coredump 导出没有可安全重放的源复制阶段；Worker 重启后把遗留运行项明确标失败。
            await repo.db.coredump_exports.update_many(
                {"coordinatorNodeId": settings.node_id, "status": "RUNNING", "leaseUntil": {"$lt": now()}},
                {"$set": {"status": "FAILED", "error": "WORKER_RESTART", "updatedAt": now()}},
            )
            from camera_logs.logs.maintenance import recover_orphan_archives
            await recover_orphan_archives(repo)
            app.state.repo, app.state.worker = repo, worker
            from camera_logs.node.files import install_node_routes
            reads = install_node_routes(app, repo, worker)
            worker_task = asyncio.create_task(worker.run())
            yield
        finally:
            try:
                if worker_task is not None:
                    worker_task.cancel()
                    await asyncio.gather(worker_task, return_exceptions=True)
                if worker is not None:
                    await worker.close()
            finally:
                try:
                    if reads is not None:
                        await reads.close()
                finally:
                    try:
                        from camera_logs.logs.compression import shutdown_compression
                        await shutdown_compression()
                    finally:
                        try:
                            if client is not None:
                                await client.close()
                        finally:
                            if listener is not None:
                                listener.stop()

    app = FastAPI(title="采集节点内部服务", lifespan=lifespan)

    @app.get("/health")
    async def health():
        """提供不依赖业务鉴权的节点存活检查。"""
        return {"status": "ok"}

    @app.get("/internal/tail/{task_id}")
    async def tail(task_id: str, request: Request, cursor: str = ""):
        """以内部令牌读取当前节点内存帧，缺少运行时返回空窗口。"""
        expected = "Bearer " + settings.internal_token
        if not settings.internal_token or not secrets.compare_digest(request.headers.get("authorization", ""), expected):
            raise HTTPException(401)
        runtime = app.state.worker.active.get(task_id)
        return runtime.tail(cursor) if runtime else {"frames": [], "gap": False}

    return app
