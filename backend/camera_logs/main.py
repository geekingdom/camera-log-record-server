"""构建公共 FastAPI 应用，统一管理依赖、路由、异常响应和后台调度生命周期。"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pymongo import AsyncMongoClient

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, public
from camera_logs.common.node_http import NodeHttpPool
from camera_logs.common.observability import redact_text


def create_app(settings=None, db=None):
    """创建可测试的 API 应用；可注入配置和数据库以隔离运行环境。"""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        """启动时验证密钥、初始化索引与调度器，关闭时按反向顺序释放资源。"""
        if not settings.encryption_key or not settings.bootstrap_token:
            raise RuntimeError("必须配置 ENCRYPTION_KEY 和 BOOTSTRAP_TOKEN；参见 .env.example")
        client = listener = None
        background = []
        try:
            # 初始化也属于资源生命周期，目录或仓库构造失败仍须关闭已取得的客户端。
            database = db
            if database is None:
                client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                                          w="majority", journal=True)
                database = client[settings.database_name]
            repo = Repository(database, settings)
            from camera_logs.common.observability import setup_logging
            listener = setup_logging(settings.log_root.parent / "service-logs" / "api")
            await repo.initialize()
            from camera_logs.users.sessions import initialize_admin
            await initialize_admin(repo)
            app.state.repo = repo
            # 固定分池降低高并发连接状态扫描成本，全部客户端归 API 生命周期管理。
            async with NodeHttpPool() as node_http:
                app.state.node_http = node_http
                if settings.start_background:
                    from camera_logs.common.record_maintenance import record_maintenance_loop
                    from camera_logs.logs.job_lease import recovery_loop
                    from camera_logs.resources.health import health_loop
                    from camera_logs.tasks.scheduler import scheduler_loop
                    background = [asyncio.create_task(scheduler_loop(repo)), asyncio.create_task(health_loop(repo)),
                                  asyncio.create_task(recovery_loop(repo)), asyncio.create_task(record_maintenance_loop(repo))]
                try:
                    yield
                finally:
                    for task in background:
                        task.cancel()
                    if background:
                        await asyncio.gather(*background, return_exceptions=True)
        finally:
            try:
                if client is not None:
                    await client.close()
            finally:
                # Mongo关闭抛错或取消也必须执行日志排空；异常链由服务管理器记录。
                if listener is not None:
                    listener.stop()

    app = FastAPI(title="设备日志记录服务", version="0.1.0", lifespan=lifespan)
    @app.middleware("http")
    async def client_network_policy(request, call_next):
        """平台来源白名单在登录前生效；健康检查与独立采集节点不受此策略影响。"""
        if request.url.path.startswith("/api/v1/"):
            from camera_logs.access_policy.policy import enforce_ip
            try:
                await enforce_ip(app.state.repo, request)
            except HTTPException as exc:
                request.state.safe_error = redact_text(str(exc.detail))
                return JSONResponse(status_code=exc.status_code, content={"error": {
                    "code": "IP_ACCESS_DENIED", "message": request.state.safe_error,
                    "requestId": getattr(request.state, "request_id", None)}})
        return await call_next(request)
    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        """将业务 HTTP 异常包装为包含 requestId 的稳定 API 错误结构。"""
        request.state.safe_error = redact_text(str(exc.detail))
        return JSONResponse(status_code=exc.status_code, headers=exc.headers, content={"error": {
            "code": getattr(exc, "code", str(exc.status_code)), "message": request.state.safe_error,
            "requestId": request.state.request_id}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        """将 Pydantic 请求校验细节转换为前端可消费的 422 结构。"""
        request.state.safe_error = "输入校验失败"
        errors = [{"location": list(e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {
            "code": "VALIDATION_ERROR", "message": "输入校验失败", "details": errors,
            "requestId": request.state.request_id}})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        """对外只返回安全说明；请求中间件记录异常类型及安全的结构化位置。"""
        request.state.safe_error = "服务内部异常"
        return JSONResponse(status_code=500, content={"error": {
            "code": "INTERNAL_ERROR", "message": "服务内部异常，请凭请求编号查询运行日志",
            "requestId": getattr(request.state, "request_id", None)}})

    def repo():
        """返回生命周期已初始化的 Repository，供路由安装函数闭包使用。"""
        return app.state.repo

    async def listing(collection, query, page, page_size, sort="createdAt"):
        """按统一分页和排序规则查询集合，并在输出前移除敏感字段。"""
        cursor = repo().db[collection].find(query).sort(sort, -1).skip((page-1)*page_size).limit(page_size)
        return {"items": [public(item) async for item in cursor],
                "total": await repo().db[collection].count_documents(query), "page": page, "pageSize": page_size}

    @app.get("/health")
    async def health():
        """检查数据库 ping，供容器健康检查和编排器探活调用。"""
        await repo().db.command("ping")
        return {"status": "ok"}

    from camera_logs.tasks.api import install_task_routes
    from camera_logs.users.api import install_user_routes
    install_user_routes(app)
    from camera_logs.access_policy.api import install_ip_policy_routes
    install_ip_policy_routes(app)
    install_task_routes(app, repo, listing)
    from camera_logs.resources.api import install_resource_routes
    install_resource_routes(app, repo, listing)
    from camera_logs.resource_metrics.api import install_resource_metric_routes
    install_resource_metric_routes(app)
    from camera_logs.commands.templates import install_template_routes
    install_template_routes(app, repo, listing)
    from camera_logs.commands.api import install_command_routes
    install_command_routes(app, repo, listing)

    from camera_logs.logs.api import install_log_routes
    install_log_routes(app)
    from camera_logs.coredumps.api import install_coredump_routes
    install_coredump_routes(app)
    from camera_logs.administration.api import install_admin_routes
    install_admin_routes(app)
    from camera_logs.administration.settings import install_settings_routes
    install_settings_routes(app)
    from camera_logs.logs.download_sessions import install_download_sessions
    install_download_sessions(app)
    from camera_logs.reference.api import install_reference_routes
    install_reference_routes(app)
    # 最后注册的 ASGI 中间件最先执行，必须覆盖来源策略提前返回的拒绝响应。
    from camera_logs.common.observability import add_request_logging
    add_request_logging(app)
    return app


app = create_app()
