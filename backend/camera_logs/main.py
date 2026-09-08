"""构建公共 FastAPI 应用，统一管理依赖、路由、异常响应和后台调度生命周期。"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pymongo import AsyncMongoClient

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, public


def create_app(settings=None, db=None):
    """创建可测试的 API 应用；可注入配置和数据库以隔离运行环境。"""
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app):
        """启动时验证密钥、初始化索引与调度器，关闭时按反向顺序释放资源。"""
        if not settings.encryption_key or not settings.bootstrap_token:
            raise RuntimeError("必须配置 ENCRYPTION_KEY 和 BOOTSTRAP_TOKEN；参见 .env.example")
        client = None
        database = db
        if database is None:
            client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                                      w="majority", journal=True)
            database = client[settings.database_name]
        repo = Repository(database, settings)
        from camera_logs.common.observability import setup_logging
        listener = setup_logging(settings.log_root.parent / "service-logs" / "api")
        await repo.initialize()
        app.state.repo = repo
        background = None
        if settings.start_background:
            from camera_logs.tasks.scheduler import scheduler_loop
            background = asyncio.create_task(scheduler_loop(repo))
        yield
        if background:
            background.cancel()
            await asyncio.gather(background, return_exceptions=True)
        if client:
            await client.close()
        listener.stop()

    app = FastAPI(title="设备日志记录服务", version="0.1.0", lifespan=lifespan)
    from camera_logs.common.observability import add_request_logging
    add_request_logging(app)
    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        """将业务 HTTP 异常包装为包含 requestId 的稳定 API 错误结构。"""
        return JSONResponse(status_code=exc.status_code, content={"error": {
            "code": str(exc.status_code), "message": exc.detail, "requestId": request.state.request_id}})

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        """将 Pydantic 请求校验细节转换为前端可消费的 422 结构。"""
        errors = [{"location": list(e["loc"]), "message": e["msg"]} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"error": {
            "code": "VALIDATION_ERROR", "message": "输入校验失败", "details": errors,
            "requestId": request.state.request_id}})

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        """完整堆栈由请求中间件记录，对外仅返回关联号而不暴露内部路径或凭据。"""
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
    install_task_routes(app, repo, listing)
    from camera_logs.commands.templates import install_template_routes
    install_template_routes(app, repo, listing)
    from camera_logs.commands.api import install_command_routes
    install_command_routes(app, repo, listing)

    from camera_logs.logs.api import install_log_routes
    install_log_routes(app)
    from camera_logs.administration.api import install_admin_routes
    install_admin_routes(app)
    from camera_logs.logs.download_sessions import install_download_sessions
    install_download_sessions(app)
    return app


app = create_app()
