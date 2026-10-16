"""ES正文副本随原始目录删除补偿，不依赖Mongo TTL直接删除索引状态。"""

import asyncio

from camera_logs.indexing.client import checked, connection


def file_page(settings, after):
    """使用composite游标有界枚举本节点ES来源，包含尚未发布证明的失败批次。"""
    composite = {"size": 256, "sources": [{"file": {"terms": {"field": "fileId"}}}]}
    if after:
        composite['after'] = after
    with connection(settings) as client:
        result = checked(client.post(f'/{settings.elasticsearch_index}/_search', json={"size": 0,
            "query": {"term": {"nodeId": settings.node_id}},
            "aggs": {"files": {"composite": composite}}}))['aggregations']['files']
        return [item['key']['file'] for item in result['buckets']], result.get('after_key')


def delete_files(settings, identifiers):
    """删除来源下全部候选和证明，不触碰原始文件；失败下轮重试。"""
    with connection(settings) as client:
        checked(client.post(f'/{settings.elasticsearch_index}/_delete_by_query',
            params={"conflicts": "proceed", "refresh": "true"}, json={"query": {"bool": {"filter": [
                {"term": {"nodeId": settings.node_id}}, {"terms": {"fileId": identifiers}},
            ]}}}))


async def prune_deleted(settings, db):
    """原始目录已删除或不存在才清副本，数据库不可用时停止删除而非猜测不存在。"""
    after = None
    while True:
        identifiers, next_after = await asyncio.to_thread(file_page, settings, after)
        if not identifiers:
            return
        existing = {file['id'] async for file in db.files.find(
            {"id": {"$in": identifiers}, "status": {"$ne": "DELETED"}}, {"id": 1})}
        removed = set(identifiers) - existing
        if removed:
            await asyncio.to_thread(delete_files, settings, sorted(removed))
        if not next_after or next_after == after:
            return
        after = next_after
