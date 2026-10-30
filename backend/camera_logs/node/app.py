"""装配采集节点内部 HTTP 服务、运行监督与进程级资源生命周期。"""

import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, Request
from pymongo import AsyncMongoClient

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.common.observability import (
    refresh_service_log_retention,
    service_log_retention_loop,
    setup_logging,
)
from camera_logs.logs.segment_size import segment_size_cache
from camera_logs.node.worker import Worker


def create_worker_app(settings=None):
    """创建节点内部服务；启动失败和正常退出都会按依赖反序释放资源。"""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        listener = client = None
        worker_task = retention_task = segment_size_task = reads = worker = None
        try:
            listener = setup_logging(settings.log_root.parent / "service-logs" / settings.node_id)
            client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                                      w="majority", journal=True)
            repo = Repository(client[settings.database_name], settings)
            await repo.initialize()
            # 每个独立节点直接读取共享配置，避免 API 进程内热更新无法传到其它主机。
            await refresh_service_log_retention(repo)
            # 采集 Writer 在锁内只读此共享内存缓存；每秒后台刷新使运行中任务的
            # 下一物理分片使用管理员最新保存的上限，不把 Mongo 往返加入整点写入。
            segment_cache = segment_size_cache(repo)
            await segment_cache.refresh(repo)
            # deploy-all 的计划重启可能与调度器的心跳扫描交错；新 Worker 先消费
            # 同节点计划窗口产生的 BLOCKED 任务，再开始新一轮调度。
            from camera_logs.node.recovery import recover_planned_restart_tasks
            await recover_planned_restart_tasks(repo, settings.node_id)
            worker = Worker(repo)
            from camera_logs.logs.job_lease import recover_expired_jobs
            await recover_expired_jobs(repo)
            # coredump 导出没有可安全重放的源复制阶段；Worker 重启后把遗留运行项明确标失败。
            await repo.db.coredump_exports.update_many(
                {"coordinatorNodeId": settings.node_id, "status": "RUNNING", "leaseUntil": {"$lt": now()}},
                {"$set": {"status": "FAILED", "error": "WORKER_RESTART", "updatedAt": now()}},
            )
            app.state.repo, app.state.worker = repo, worker
            from camera_logs.node.files import install_node_routes
            reads = install_node_routes(app, repo, worker)
            from camera_logs.node.search_index_routes import install_search_index_routes
            install_search_index_routes(app, repo)
            # 历史归档校验可耗时较长，复用周期维护槽在后台恢复，避免拖住所有新采集。
            worker.start_maintenance()
            worker_task = asyncio.create_task(worker.run())
            retention_task = asyncio.create_task(service_log_retention_loop(repo))
            segment_size_task = asyncio.create_task(segment_cache.loop(repo))
            yield
        finally:
            try:
                if retention_task is not None:
                    retention_task.cancel()
                    await asyncio.gather(retention_task, return_exceptions=True)
                if segment_size_task is not None:
                    segment_size_task.cancel()
                    await asyncio.gather(segment_size_task, return_exceptions=True)
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
                        await shutdown_compression(timeout_seconds=5)
                    finally:
                        try:
                            from camera_logs.logs.write_io import shutdown as shutdown_write_io
                            await shutdown_write_io()
                        finally:
                            try:
                                if client is not None:
                                    await client.close()
                            finally:
                                if listener is not None:
                                    listener.stop()

    app = FastAPI(title="采集节点内部服务", lifespan=lifespan)
    from camera_logs.node.slave_routes import install_slave_routes
    install_slave_routes(app, settings)
    from camera_logs.node.failover import install_failover_routes
    install_failover_routes(app, settings)

    @app.get("/health")
    async def health():
        """提供不依赖业务鉴权的节点存活检查。"""
        return {"status": "ok"}

    @app.get("/internal/tail/{task_id}")
    async def tail(task_id: str, request: Request, cursor: str = "",
                   waitSeconds: float = Query(0, ge=0, le=1,
                                              description="内部实时 tail 在无新帧时最多等待的秒数，0 表示立即返回，最大 1 秒。")):
        """以内部令牌读取当前节点内存帧，可有界等待新帧，缺少运行时立即返回空窗口。"""
        expected = "Bearer " + settings.internal_token
        if not settings.internal_token or not secrets.compare_digest(request.headers.get("authorization", ""), expected):
            raise HTTPException(401)
        runtime = app.state.worker.active.get(task_id)
        if runtime is None:
            return {"frames": [], "gap": False}
        result = await runtime.wait_tail(cursor, waitSeconds)
        # 等待期间运行实例被停止或被同任务的新实例替换时，旧缓冲不能继续对外发布。
        if app.state.worker.active.get(task_id) is not runtime:
            return {"frames": [], "gap": False}
        return result

    return app
