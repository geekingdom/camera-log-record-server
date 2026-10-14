"""海康 HTTP 凭据失败后的采集新建连接门控。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from pymongo.errors import PyMongoError

from camera_logs.common.ownership import owner_filter

logger = logging.getLogger(__name__)


async def wait_for_resource_authentication(repo, task, *, stopping: Callable[[], bool], sleep=asyncio.sleep) -> bool:
    """等待本运行的海康认证锁存清除，读取异常保守等待且绝不建立新连接。

    仅 SSH/Telnet 设备的海康资源受此门控；串口服务不依赖设备 HTTP 认证，保持原有行为。
    每次轮询都复核 owner 与 RUNNING 意图，因此用户停止、暂停或后继运行不会被旧实例唤醒。
    """
    if task.get("protocol") == "TELNET_SERIAL":
        return True
    while not stopping():
        try:
            # 单次 Mongo 调用卡住时不能让本地运行越过认证保护；限定整个快照窗口后重试。
            async with asyncio.timeout(2):
                current = await repo.db.tasks.find_one(
                    {**owner_filter(task), "desiredState": "RUNNING"}, {"id": 1},
                )
                if current is None:
                    return False
                resource = await repo.db.resources.find_one(
                    {"id": task.get("resourceId"), "deletedAt": None},
                    {"kind": 1, "healthStatus": 1, "authRejectedAt": 1},
                )
            if resource is None:
                # 历史任务可引用已移除的非海康资源；缺少 HTTP 健康模型不能据此改变其连接语义。
                return True
            if resource.get("kind") != "HIKVISION_NETWORK":
                return True
            # authRejectedAt 由健康事务在 401 时写入，仅 SUCCESS 清除。它使 401 后的
            # ERROR/OFFLINE 保持阻塞，并跨节点接管或运行时重启保留同一个事实。
            if resource.get("authRejectedAt") is None and resource.get("healthStatus") != "AUTH_FAILED":
                return True
        except (PyMongoError, TimeoutError):
            logger.exception("认证连接门控查询失败，保守等待 task=%s", task.get("id"))
        await sleep(1)
    return False
