"""采集模块的数据与连接合同，隔离传输实现、日志通知和回调调用细节。"""

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


class AsyncConnection(Protocol):
    """单会话异步传输接口；接收器独占 read，发送队列独占 write，结束必须 close。"""

    async def read(self, size: int = 65536) -> bytes:
        """返回下一段原始字节；空字节表示连接结束。"""
        ...

    async def write(self, data: bytes) -> None:
        """按给定顺序发送字节，无法确认时向调用方抛出异常。"""
        ...

    async def close(self) -> None:
        """释放连接资源并使正在等待的接收操作结束。"""
        ...


@dataclass(frozen=True, slots=True)
class LogChunk:
    """不可变的已写入日志通知，绑定原任务、运行、会话与文件字节位置。"""

    task_id: str
    run_id: str
    session_id: str
    sequence: int
    data: bytes
    offset: int
    path: str


Callback = Callable[..., Awaitable[Any] | Any]


async def call_callback(callback: Callback | None, *args: Any) -> Any:
    """统一调用可选同步或异步回调，不吞掉持久化和状态发布异常。"""
    if callback is None:
        return None
    value = callback(*args)
    return await value if inspect.isawaitable(value) else value
