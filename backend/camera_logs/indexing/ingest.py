"""独立索引进程的文件回放：批次确认后提交位置，完整摘要确认后发布覆盖证明。"""

import hashlib
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from camera_logs.common.database import now
from camera_logs.common.presentation_time import SHANGHAI, utc_instant
from camera_logs.indexing.checkpoints import Checkpoints
from camera_logs.indexing.client import CHUNK_BYTES, OVERLAP, bulk, checked, connection, evidence, fingerprint
from camera_logs.logs.archive_access import read_limiter
from camera_logs.logs.search_stream import IndexCursor, index_entries
from camera_logs.logs.source import open_log_source


def source_path(settings, file):
    """只从当前节点日志根目录读取，目录中的绝对路径也必须落在根目录内。"""
    root = Path(settings.log_root).resolve()
    path = Path(file['path'])
    path = (path if path.is_absolute() else root / path).resolve()
    if root not in path.parents:
        raise ValueError('索引来源超出节点日志目录')
    return path


def _shanghai(value):
    """ES源文档时间使用上海ISO，缺失接收时间保持null而非猜测。"""
    if value is None:
        return None
    stamp = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return utc_instant(stamp).astimezone(SHANGHAI).isoformat()


def index_stream(settings, file, stream, size, *, count_reads=True):
    """把已打开且冻结的正文流写入 ES；未完成来源不发布覆盖证明。

    重启后重读前缀核对摘要但不重复发送已确认批次。若ES丢失部分已确认块则从零
    幂等重放；归档调用方统一计量解压后的逻辑输入，原始文件按正文读取计量。
    """
    revision = fingerprint(file)
    checkpoints = Checkpoints(settings.elasticsearch_checkpoint_path)
    try:
        with connection(settings) as client:
            index = settings.elasticsearch_index
            if evidence(client, index, [file]):
                return False
            state = checkpoints.get(revision)
            count = checked(client.post(f'/{index}/_count', json={"query": {"bool": {"filter": [
                {"term": {"revision": revision}}, {"term": {"kind": "chunk"}},
                {"range": {"offset": {"lt": state['offset']}}},
            ]}}}))['count']
            if count != state['chunks']:
                state = {"offset": 0, "chunks": 0}
            digest, offset, chunks = hashlib.sha256(), 0, 0
            head, tail, newline = b'', b'', False
            stamp = file.get('firstReceivedAt')
            stamp = _shanghai(stamp)
            base = {key: str(file.get(key) or '') for key in
                    ('taskId', 'resourceId', 'nodeId', 'runId', 'sessionId')}
            base.update(fileId=file['id'], revision=revision, hour=_shanghai(file['hour']))
            with ExitStack() as resources:
                entries = []
                if file.get('indexPath'):
                    index_path = source_path(settings, {'path': file['indexPath']})
                    if index_path.is_file():
                        index_stream = resources.enter_context(index_path.open('rb'))
                        entries = index_entries(index_stream, lambda: False)
                cursor = IndexCursor(entries)
                if size != file['bytes']:
                    raise ValueError('已封存文件水位不一致')
                batch = []
                while data := stream.read(CHUNK_BYTES):
                    if count_reads:
                        read_limiter.consume(len(data))
                    digest.update(data)
                    if offset == 0:
                        head = data[:OVERLAP]
                    newline |= b'\n' in data
                    # 前16字节重叠保证任何三字符UTF-8前缀都落入一个候选块。
                    text = (tail + data).decode('utf-8', 'replace')
                    if offset >= state['offset']:
                        identifier = hashlib.sha256(f'{revision}:{offset}'.encode()).hexdigest()
                        entry = cursor.locate(offset)
                        received = entry.get('receivedAt') if entry else None
                        doc = base | {"kind": "chunk", "offset": offset, "length": len(data),
                                      "text": text, "receivedAt": _shanghai(received),
                                      "indexedAt": now().astimezone(SHANGHAI).isoformat()}
                        # 文本有重叠，精确定位使用offset/length回读原文，不按text字符下标推算。
                        batch.append((identifier, doc))
                    offset += len(data)
                    chunks += 1
                    tail = (tail + data)[-OVERLAP:]
                    if len(batch) >= settings.elasticsearch_batch_chunks:
                        bulk(client, index, batch)
                        checkpoints.save(revision, {"offset": offset, "chunks": chunks})
                        batch = []
                if batch:
                    bulk(client, index, batch)
                    checkpoints.save(revision, {"offset": offset, "chunks": chunks})
            if offset != file['bytes'] or not file.get('sha256') or digest.hexdigest() != file['sha256']:
                raise ValueError('索引来源摘要不符，不发布覆盖证明')
            checked(client.post(f'/{index}/_refresh'))
            actual = checked(client.post(f'/{index}/_count', json={"query": {"bool": {"filter": [
                {"term": {"revision": revision}}, {"term": {"kind": "chunk"}},
            ]}}}))['count']
            if actual != chunks:
                raise ValueError('索引分块数量不符')
            proof = base | {"kind": "manifest", "bytes": offset, "chunks": chunks,
                            "headHex": head.hex(), "tailHex": tail.hex(), "hasNewline": newline,
                            "indexedAt": now().astimezone(SHANGHAI).isoformat(), "receivedAt": stamp}
            checked(client.put(f'/{index}/_doc/manifest-{revision}', params={"refresh": "wait_for"}, json=proof))
            checkpoints.save(revision, {"offset": offset, "chunks": chunks})
            return True
    finally:
        checkpoints.close()


def index_file(settings, file):
    """打开原始或单个归档成员后回放；多成员归档由 archive 模块复用同一解压流。"""
    path = source_path(settings, file)
    with open_log_source(path, file.get('archiveMember')) as source:
        return index_stream(settings, file, source.stream, source.size)


def within_hot_window(file, cutoff):
    """热索引以日志所属小时计时，重建不延长历史正文副本的保留期。"""
    try:
        return datetime.fromisoformat(file['hour']) >= cutoff
    except (KeyError, ValueError, TypeError):
        return False
