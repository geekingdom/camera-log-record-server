"""记录实时订阅的生命周期及失败类别，绝不读取令牌帧、日志正文或关闭原因文本。"""

import asyncio
import logging
import time
import uuid
from collections.abc import Mapping


async def track_websocket(app, scope, receive, send, logger):
    """观察 ASGI 控制消息，在断开或失败时生成一条带身份关联的访问记录。"""
    state = scope.setdefault("state", {})
    state["request_id"] = uuid.uuid4().hex
    started = time.perf_counter()
    accepted, close_code, frames, error_type = False, None, 0, None

    async def tracked_receive():
        nonlocal close_code
        message = await receive()
        if message["type"] == "websocket.disconnect":
            close_code = message.get("code", 1006)
        return message

    async def tracked_send(message):
        nonlocal accepted, close_code, frames
        await send(message)
        if message["type"] == "websocket.accept":
            accepted = True
        elif message["type"] == "websocket.close":
            close_code = message.get("code", 1000)
        elif message["type"] == "websocket.send":
            frames += 1

    try:
        await app(scope, tracked_receive, tracked_send)
    except (Exception, asyncio.CancelledError) as error:
        # 异常类型可定位故障类别，异常正文可能包含首帧令牌或设备输出，不进入访问日志。
        error_type = type(error).__name__
        raise
    finally:
        actor = state.get("actor")
        actor_id = actor.get("id") if isinstance(actor, Mapping) else actor
        error_type = error_type or state.get("failure_type")
        context = {
            "requestId": state["request_id"], "actor": actor_id, "method": "WEBSOCKET",
            "route": getattr(scope.get("route"), "path", None) or scope.get("path"),
            "targets": dict(scope.get("path_params", {})), "accepted": accepted,
            "closeCode": close_code if close_code is not None else 1006,
            "framesSent": frames, "errorType": error_type,
            "durationMs": round((time.perf_counter() - started) * 1000, 3),
        }
        level = logging.WARNING if error_type or context["closeCode"] not in {1000, 1001} else logging.INFO
        logger.log(level, "websocket ended", extra={"context": context})
