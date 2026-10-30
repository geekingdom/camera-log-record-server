"""实时日志 WebSocket 帧的唯一公开结构定义。"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from camera_logs.common.response_models import StrictResponseModel


class DataFrame(StrictResponseModel):
    """一段可按文件字节位置续传的实时原始日志帧。"""

    type: Literal["data"] = Field(description="固定为 data，表示原始日志字节帧。")
    fileId: str = Field(description="承载该字节范围的不可变日志文件标识。")
    sessionId: str = Field(description="写入该日志文件的采集会话标识。")
    runId: str = Field(description="产生此帧的任务运行标识。")
    offset: int = Field(ge=0, description="半开字节区间 [offset, endOffset) 的起始位置。")
    endOffset: int = Field(ge=0, description="半开字节区间 [offset, endOffset) 的结束位置。")
    data: str = Field(description="该区间原始字节的 Base64 编码，不是 UTF-8 日志文本。")
    size: int = Field(ge=0, description="data Base64 解码后的原始字节数。")
    cursor: str = Field(description="服务端续传游标；不透明，客户端只能原样回传。")


class GapFrame(StrictResponseModel):
    """实时环形缓冲已淘汰请求游标时发送的补读提示。"""

    type: Literal["gap"] = Field(description="固定为 gap，表示请求的实时缓冲已过期。")
    message: str = Field(description="面向用户的补读说明，应通过小时归档恢复缺失内容。")


class StatusFrame(StrictResponseModel):
    """未结束且尚无分配节点的任务状态；暂停订阅保持连接等待恢复。"""

    type: Literal["status"] = Field(description="固定为 status，表示没有可转发的实时数据。")
    status: str = Field(description="当前未结束任务状态；PAUSED 表示订阅保持连接等待恢复。软删除或已物理停止的任务不发送此帧，而以 WebSocket 1000 正常关闭。")


class SubscribeFrame(StrictResponseModel):
    """客户端建立实时日志订阅时发送的唯一首帧；后续断开立即取消上游操作。"""

    token: str | None = Field(default=None, description="可选服务令牌；省略或空字符串时使用现有 Cookie 会话鉴权。")
    cursor: str | None = Field(default="", description="可选不透明续传游标；null 或省略时从当前实时缓冲的起点订阅。")


SUBSCRIBE_SCHEMA = SubscribeFrame.model_json_schema()
