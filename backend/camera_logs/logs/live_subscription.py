"""实时订阅中客户端断开与上游操作的可取消等待。"""

import asyncio
from collections.abc import Awaitable

from fastapi import WebSocket, WebSocketDisconnect


async def watch_disconnect(socket: WebSocket) -> int:
    """持续消费首帧后的入站事件，只在客户端明确离开时返回关闭码。

    实时日志协议只有首帧可携带订阅参数；之后的普通入站帧不改变订阅，
    但必须继续读取，才能及时观察 ASGI 的 websocket.disconnect 事件。
    """
    try:
        while True:
            event = await socket.receive()
            if event["type"] == "websocket.disconnect":
                return int(event.get("code") or 1000)
    except WebSocketDisconnect as error:
        return error.code


async def await_live_operation(operation: Awaitable, disconnected: asyncio.Task[int]):
    """等待一项订阅操作，并由客户端断开优先取消该操作，不自行重试。

    节点 tail、数据库读取和鉴权均在调用端作为 operation 传入。断开先于
    或同时发生时，先取消 operation 并等待其清理，保证不遗留 HTTP 轮询。
    """
    pending = asyncio.ensure_future(operation)
    try:
        done, _ = await asyncio.wait({pending, disconnected}, return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            # 即使上游恰好同时失败，也必须读取结果，避免遗留未观测的 Task 异常。
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            raise WebSocketDisconnect(code=disconnected.result())
        return pending.result()
    except BaseException:
        if not pending.done():
            pending.cancel()
        # 外层取消也可能与上游失败同轮发生，已完成的异常同样必须取回。
        await asyncio.gather(pending, return_exceptions=True)
        raise
