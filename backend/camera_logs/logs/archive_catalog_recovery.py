"""独立于全文索引和历史校验的轻量目录对账，单项失败不饿死其它小时。"""

import asyncio
import logging
from pathlib import Path

from camera_logs.logs.archive_receipts import acknowledge, load_verified
from camera_logs.logs.job_threads import job_thread

logger = logging.getLogger(__name__)
BATCH_SIZE = 8
CATALOG_TIMEOUT_SECONDS = 5
_cursor: dict[str, str] = {}


async def reconcile_receipts(repo, receipts, register):
    """每轮最多八包循环推进；进程重启从持久receipt继续，更新成功后才确认。"""
    key = str(Path(repo.settings.log_root).resolve())
    previous = _cursor.get(key, '')
    candidates = sorted(receipts, key=lambda path: (str(path) <= previous, str(path)))[:BATCH_SIZE]
    completed = set()
    for receipt in candidates:
        _cursor[key] = str(receipt)
        target = Path(str(receipt).removesuffix('.catalog.pending.json'))
        try:
            members, size, version = await job_thread(load_verified, target)
            # 已校验文件的目录补偿只有数据库更新需要超时；超时保留待办供下轮幂等重试。
            async with asyncio.timeout(CATALOG_TIMEOUT_SECONDS):
                await register(repo, target, members, size)
            await job_thread(acknowledge, target, version)
            completed.add(target.resolve())
        except asyncio.CancelledError:
            raise
        except FileNotFoundError:
            # 正常保留期清理或另一轮发布竞争由后续扫描/历史恢复复核。
            continue
        except Exception as error:  # noqa: BLE001 - 单包异常保留待办，继续其它已校验小时。
            logger.warning('归档目录待办未确认 target=%s errorType=%s', target, type(error).__name__)
    return completed
