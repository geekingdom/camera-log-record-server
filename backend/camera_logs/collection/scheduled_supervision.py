"""监督定时后台协程，确保不可恢复异常可见并触发原会话收尾。"""

import asyncio
import logging

logger = logging.getLogger(__name__)


async def supervise_scheduled(collector, position, item):
    """普通命令失败由调度循环处理；未处理异常禁发并关闭连接，不等待自身停止。"""
    try:
        await collector._scheduled_loop(position, item)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        logger.exception("定时命令后台执行失败 task=%s run=%s session=%s command=%s",
                         collector.task_id, collector.run_id, collector.session_id,
                         item.get("id", f"scheduled-{position}"))
        collector._terminal_error = error
        collector._accepting_commands = False
        try:
            # reader 的正常退出路径负责排空缓冲与归档；这里不能调用 stop 自我等待。
            await collector._close_connection()
        except Exception:
            logger.exception("定时异常后关闭连接失败 task=%s session=%s",
                             collector.task_id, collector.session_id)
