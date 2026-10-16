"""每节点独立索引服务入口；ES或磁盘失败只影响本进程，绝不调用采集控制接口。"""

import argparse
import asyncio
import fcntl
import logging
from datetime import timedelta

import httpx
from pymongo import AsyncMongoClient

from camera_logs.common.config import Settings
from camera_logs.common.database import now
from camera_logs.indexing.archive import archive_key, visit_archive_members
from camera_logs.indexing.checkpoints import Checkpoints
from camera_logs.indexing.client import checked, connection, ensure_index, evidence
from camera_logs.indexing.ingest import index_file, index_stream, within_hot_window
from camera_logs.indexing.retention import prune_deleted
from camera_logs.logs.archive_access import read_limiter

logger = logging.getLogger(__name__)
ARCHIVE_MEMBER_LIMIT = 4096


def cleanup(settings, cutoff):
    """正文和覆盖证明使用同一小时规则删除，ES故障时下一轮继续补偿。"""
    checkpoints = Checkpoints(settings.elasticsearch_checkpoint_path)
    try:
        checkpoints.prune(settings.elasticsearch_retention_days)
    finally:
        checkpoints.close()
    with connection(settings) as client:
        checked(client.post(f'/{settings.elasticsearch_index}/_delete_by_query',
                            params={"conflicts": "proceed", "refresh": "true"},
                            json={"query": {"bool": {"filter": [
                                {"term": {"nodeId": settings.node_id}},
                                {"range": {"hour": {"lt": cutoff.isoformat()}}},
                            ]}}}))


def index_archive_files(settings, files):
    """同包已完整覆盖时不打开归档；其余成员共用一次顺序 gzip 解压。"""
    with connection(settings) as client:
        covered = set(evidence(client, settings.elasticsearch_index, files))
    pending = [file for file in files if file['id'] not in covered]
    if not pending:
        return {file['id']: False for file in files}
    return visit_archive_members(
        settings, pending,
        lambda file, stream, size: index_stream(settings, file, stream, size, count_reads=False),
    ) | {file['id']: False for file in files if file['id'] in covered}


async def add_task_resource_ids(db, files):
    """目录旧记录未复制资源时只补 ES 展示字段，不参与来源版本指纹。"""
    task_ids = {file.get('taskId') for file in files if not file.get('resourceId') and file.get('taskId')}
    if not task_ids:
        return files
    resources = {task['id']: task.get('resourceId') async for task in db.tasks.find(
        {'id': {'$in': list(task_ids)}}, {'id': 1, 'resourceId': 1}
    )}
    return [file | ({'resourceId': resources[file['taskId']]} if resources.get(file.get('taskId')) else {}) for file in files]


async def full_archive_group(db, settings, file):
    """首次遇到小时包时跨普通分页取全体成员，显式上限避免单目录无限占用内存。"""
    files = await db.files.find({
        'nodeId': settings.node_id, 'status': 'READY', 'path': file['path'],
    }).sort('id', 1).limit(ARCHIVE_MEMBER_LIMIT + 1).to_list(length=ARCHIVE_MEMBER_LIMIT + 1)
    if len(files) > ARCHIVE_MEMBER_LIMIT:
        raise ValueError('单小时归档成员超过旁路索引上限')
    return await add_task_resource_ids(db, files)


async def run(settings, *, once=False):
    """每次从元数据分页巡回，失败项不阻塞其他文件，SQLite保存逐文件确认位置。"""
    if not settings.elasticsearch_url:
        raise ValueError('必须配置ELASTICSEARCH_URL')
    settings.elasticsearch_checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    with settings.elasticsearch_checkpoint_path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        async with AsyncMongoClient(settings.mongo_uri, tz_aware=True, serverSelectionTimeoutMS=5000) as mongo:
            db = mongo[settings.database_name]
            while True:
                try:
                    await asyncio.to_thread(initialize, settings)
                    cutoff = now() - timedelta(days=settings.elasticsearch_retention_days)
                    await asyncio.to_thread(cleanup, settings, cutoff)
                    await prune_deleted(settings, db)
                    # 从时间较新的文件开始不应饿死旧文件：稳定id分页覆盖所有目录项。
                    cursor = None
                    processed_archives = set()
                    while True:
                        query = {"nodeId": settings.node_id, "status": "READY"}
                        if cursor:
                            query['id'] = {"$gt": cursor}
                        files = await db.files.find(query).sort('id', 1).limit(64).to_list(length=64)
                        if not files:
                            break
                        files = await add_task_resource_ids(db, files)
                        eligible = []
                        for file in files:
                            cursor = file['id']
                            if not within_hot_window(file, cutoff) or not file.get('sha256'):
                                continue
                            eligible.append(file)
                        grouped, singles = {}, []
                        for file in eligible:
                            key = archive_key(settings, file)
                            if key is None:
                                singles.append(file)
                            else:
                                grouped.setdefault(key, file)
                        for key, first in grouped.items():
                            if key in processed_archives:
                                continue
                            processed_archives.add(key)
                            try:
                                group = [file for file in await full_archive_group(db, settings, first)
                                         if within_hot_window(file, cutoff) and file.get('sha256')]
                                if not group:
                                    continue
                                results = await asyncio.to_thread(index_archive_files, settings, group)
                            except (httpx.TransportError, httpx.HTTPStatusError):
                                # 服务不可达时停止本轮，不能对每个文件重复等待网络超时。
                                raise
                            except Exception as error:  # noqa: BLE001 - 单文件及单轮索引故障不能终止后续补偿。
                                logger.warning('旁路索引归档失败 archive=%s error=%s', key, type(error).__name__)
                            else:
                                for file in group:
                                    result = results.get(file['id'])
                                    if isinstance(result, Exception):
                                        logger.warning('旁路索引失败 file=%s error=%s', file['id'], type(result).__name__)
                            await asyncio.sleep(settings.elasticsearch_file_pause_seconds)
                        for file in singles:
                            try:
                                await asyncio.to_thread(index_file, settings, file)
                            except (httpx.TransportError, httpx.HTTPStatusError):
                                raise
                            except Exception as error:  # noqa: BLE001 - 单文件及单轮索引故障不能终止后续补偿。
                                logger.warning('旁路索引失败 file=%s error=%s', file['id'], type(error).__name__)
                            await asyncio.sleep(settings.elasticsearch_file_pause_seconds)
                except Exception as error:  # noqa: BLE001 - 单文件及单轮索引故障不能终止后续补偿。
                    logger.warning('旁路索引轮次失败 error=%s', type(error).__name__)
                if once:
                    return
                await asyncio.sleep(settings.elasticsearch_poll_seconds)


def initialize(settings):
    """连接ES并安装本版本字段合同。"""
    read_limiter.configure(settings.elasticsearch_read_mib_per_second * 1024 * 1024)
    with connection(settings) as client:
        ensure_index(client, settings.elasticsearch_index)


def main():
    """支持一次性补建或独立常驻；退出不改变Worker和采集任务状态。"""
    parser = argparse.ArgumentParser(description='独立日志旁路索引进程')
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(Settings(), once=args.once))


if __name__ == '__main__':
    main()
