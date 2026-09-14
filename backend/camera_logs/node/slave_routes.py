"""跨Worker借用主机连接的内部接口，限定固定扩展服务命令并实时验证请求任务身份。"""

import asyncio
import secrets

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from camera_logs.collection.slave_ssh import bootstrap_on_host
from camera_logs.common.ownership import OwnershipLost

REMOTE_BOOTSTRAP_EXECUTION_SECONDS = 45


class BootstrapRequest(BaseModel):
    """内部调用只能指定已有任务运行和允许端口，不能携带任意命令或设备口令。"""
    model_config = ConfigDict(extra="forbid")
    taskId: str = Field(min_length=1, max_length=64)
    runId: str = Field(min_length=1, max_length=64)
    generation: int
    port: int = Field(ge=18080, le=18084)
    bootstrapToken: str = Field(min_length=32, max_length=32)


def install_slave_routes(app, settings):
    """挂载内部鉴权接口；业务访问者不能通过平台API调用节点管理命令。"""
    @app.post("/internal/slave-ssh/{host_id}")
    async def bootstrap(host_id: str, body: BootstrapRequest, request: Request):
        expected = "Bearer " + settings.internal_token
        if not settings.internal_token or not secrets.compare_digest(request.headers.get("authorization", ""), expected):
            raise HTTPException(401)
        worker = app.state.worker
        task = await worker.repo.db.tasks.find_one({"id": body.taskId, "runId": body.runId, "generation": body.generation})
        if task is None:
            raise HTTPException(409, "从机任务运行已失效")
        try:
            # 整体上限包含进入主机唯一命令队列的等待，且短于资源120秒租约。
            # 超时取消该请求持有的命令future，调用方收到503后保留租约而非临时重发。
            async with asyncio.timeout(REMOTE_BOOTSTRAP_EXECUTION_SECONDS):
                result = await bootstrap_on_host(worker, host_id, task | {"_bootstrapToken": body.bootstrapToken}, body.port)
        except OwnershipLost:
            raise HTTPException(409, "主机连接当前不可借用") from None
        except (ConnectionError, TimeoutError):
            raise HTTPException(503, "主机引导结果未知，请等待资源引导租约到期") from None
        return {"bootstrapped": result}
