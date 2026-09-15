"""增长记录独立维护循环，不阻塞节点心跳、设备认证或采集任务调度。"""

import asyncio
import logging
import time

from camera_logs.common.record_archive import maintain_growth_records
from camera_logs.common.retention_config import retention_config

logger = logging.getLogger(__name__)


async def record_maintenance_loop(repo):
    """每五秒重新读取管理员策略，单轮限制30秒；取消时事务回滚并释放维护租约。"""
    legacy_checked = float("-inf")
    while True:
        try:
            async with asyncio.timeout(30):
                platform = await repo.db.platform_settings.find_one({"id": "platform"}) or {}
                config = retention_config(platform.get("recordRetention"))
                if time.monotonic() - legacy_checked >= 3600:
                    # 不替历史记录造日期；检查命中后指向既有有界回填工具，避免静默遗漏。
                    for collection in ("audit", "events", "operations"):
                        legacy = await repo.db[collection].find_one({"createdAt": None}, {"_id": 1})
                        if legacy:
                            logger.warning("增长记录缺少创建时间，已保护 collection=%s；events可使用scripts/backfill_runtime_event_time.py预览并回填，其它记录需核实时间来源", collection)
                    legacy_checked = time.monotonic()
                result = await maintain_growth_records(repo, config)
                if any(result.get(key, 0) for key in ("archivedRuns", "partialRuns", "audit", "events", "operations")):
                    logger.info("增长记录维护完成 result=%s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("增长记录维护本轮失败，下一轮重新检查持久状态")
        await asyncio.sleep(5)
