"""ES候选查询与覆盖状态：缺失证据意味着原文补查，而不是没有日志。"""

import asyncio
import logging
import threading
import time

from camera_logs.indexing.client import connection, evidence

logger = logging.getLogger(__name__)


def fetch_evidence(settings, files, keyword=None, stop=None):
    """每批至多128个来源版本，避免terms桶截断或单请求体无限增长。"""
    result = {}
    deadline = time.monotonic() + 10
    with connection(settings) as client:
        for start in range(0, len(files), 128):
            if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
                break
            result.update(evidence(client, settings.elasticsearch_index, files[start:start + 128], keyword))
    return result


async def query_evidence(settings, files, keyword=None):
    """ES超时直接返回未知；线程只读且HTTP有超时，不修改作业或采集状态。"""
    if not getattr(settings, 'elasticsearch_url', ''):
        return {}, 'DISABLED'
    ready = [file for file in files if file.get('status') == 'READY' and file.get('sha256')]
    if not ready:
        return {}, 'AVAILABLE'
    stop = threading.Event()
    try:
        async with asyncio.timeout(10):
            return await asyncio.to_thread(fetch_evidence, settings, ready, keyword, stop), 'AVAILABLE'
    except Exception as error:  # noqa: BLE001 - 任意索引证据失败均回退原文，取消仍向上传播。
        logger.warning('ES候选不可用，回退原始日志 error=%s', type(error).__name__)
        return {}, 'UNAVAILABLE'

    finally:
        # 超时/取消后禁止线程继续遍历后续批次；当前HTTP请求由自身超时收尾。
        stop.set()
