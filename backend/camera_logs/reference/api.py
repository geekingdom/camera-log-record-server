"""内置接口目录仅依赖本服务定义，不访问外部站点或生产业务数据。"""

from typing import Annotated

from fastapi import Depends, Request

from camera_logs.common.security import actor
from camera_logs.reference.catalog import catalog


def install_reference_routes(app):
    """登录用户和有效服务Token均可读取文档，实际接口仍独立执行权限校验。"""
    @app.get("/api/v1/api-reference")
    async def api_reference(request: Request, user: Annotated[dict, Depends(actor)]):
        """返回本版本全部公共HTTP接口及实时WebSocket的参数、权限和虚拟示例。"""
        if not hasattr(request.app.state, "reference_catalog"):
            request.app.state.reference_catalog = catalog(request.app)
        return request.app.state.reference_catalog
