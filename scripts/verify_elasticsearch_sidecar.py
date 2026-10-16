"""真实ES隔离验收：稳定ID重放、分块边界字面候选、缺块回退、归档重建和热保留。"""

import hashlib
import json
import os
import tarfile
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import now
from camera_logs.indexing.checkpoints import Checkpoints
from camera_logs.indexing.client import (
    CHUNK_BYTES,
    bulk,
    checked,
    connection,
    ensure_index,
    evidence,
    fingerprint,
)
from camera_logs.indexing.ingest import index_file
from camera_logs.indexing.runner import cleanup
from camera_logs.logs.search_plan import plan_candidate_files


def main():
    """只使用随机索引与临时日志，finally清理，不访问真实业务Mongo或设备。"""
    index = 'camera-verify-' + uuid4().hex
    with tempfile.TemporaryDirectory(prefix='camera-es-verify-') as directory:
        root = Path(directory)
        settings = Settings(elasticsearch_url=os.getenv('VERIFY_ES_URL', 'http://127.0.0.1:19200'),
            elasticsearch_index=index, elasticsearch_checkpoint_path=root / 'checkpoints.sqlite',
            log_root=root, node_id='isolated', elasticsearch_batch_chunks=1)
        # UTF-8关键词穿过32KiB边界；*、?必须作为字面字符而非通配符。
        data = b'x' * (CHUNK_BYTES - 2) + '异常错误 *?literal\nsecond ERROR line\n'.encode()
        path = root / 'part.log'
        path.write_bytes(data)
        file = {"id": 'source', "taskId": 'task', "resourceId": 'resource', "nodeId": 'isolated',
                "runId": 'run', "sessionId": 'session', "hour": now().replace(minute=0, second=0, microsecond=0).isoformat(),
                "firstReceivedAt": now().isoformat(), "path": str(path), "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(), "status": 'READY'}
        with connection(settings) as client:
            try:
                ensure_index(client, index)
                # ES已写入但客户端未提交检查点：重启必须使用稳定ID安全重放。
                def acknowledged_then_lost(client, index, batch):
                    bulk(client, index, batch)
                    raise ConnectionError('simulated lost acknowledgement')
                with patch('camera_logs.indexing.ingest.bulk', acknowledged_then_lost):
                    try:
                        index_file(settings, file)
                    except ConnectionError:
                        pass
                    else:
                        raise AssertionError('ack fault not raised')
                checkpoint = Checkpoints(settings.elasticsearch_checkpoint_path)
                assert checkpoint.get(fingerprint(file))['offset'] == 0
                checkpoint.close()
                assert evidence(client, index, [file]) == {}
                assert index_file(settings, file)
                assert not index_file(settings, file), '重放完整来源不新增文档'
                for word in ('异常错误', '*?literal', 'ERROR'):
                    proof = evidence(client, index, [file], word)
                    assert proof['source']['mayMatch'], word
                assert not evidence(client, index, [file], 'NOT_FOUND')['source']['mayMatch']
                # 随机删除一个候选块，完成证明不能再被用作排除证据。
                hit = checked(client.post(f'/{index}/_search', json={"query": {"term": {"kind": 'chunk'}}, "size": 1}))['hits']['hits'][0]
                checked(client.delete(f'/{index}/_doc/{hit["_id"]}', params={"refresh": 'true'}))
                proof = evidence(client, index, [file], 'NOT_FOUND')
                assert proof == {} and plan_candidate_files([file], proof, 'NOT_FOUND') == {'source'}
                assert index_file(settings, file), '缺块后必须幂等补回'
                # 原文进入归档后删除全部ES，可从同一源版本重建。
                archive = root / 'hour.tar.gz'
                with tarfile.open(archive, 'w:gz') as output:
                    output.add(path, arcname=path.name)
                path.unlink()
                checked(client.delete('/' + index))
                ensure_index(client, index)
                file.update(path=str(archive), archiveMember='part.log')
                assert index_file(settings, file)
                assert evidence(client, index, [file], '异常错误')['source']['mayMatch']
                # 已损坏来源不能发布完成证明，即使文本块写入成功。
                damaged = file | {'id': 'damaged', 'sha256': '0' * 64}
                try:
                    index_file(settings, damaged)
                except ValueError:
                    pass
                else:
                    raise AssertionError('digest mismatch accepted')
                assert evidence(client, index, [damaged]) == {}
                # 有界选择性合成样本：比较候选回读与全扫描的字面结果及逻辑字节数。
                samples = []
                for number in range(16):
                    payload = b'normal log line 1234567890\n' * 10000
                    if number == 7:
                        payload += b'UNIQUE_FAILURE marker\n'
                    sample_path = root / f'sample-{number}.log'
                    sample_path.write_bytes(payload)
                    sample = file | {'id': f'sample-{number}', 'path': str(sample_path),
                                     'bytes': len(payload), 'sha256': hashlib.sha256(payload).hexdigest()}
                    sample.pop('archiveMember', None)
                    index_file(settings, sample)
                    samples.append(sample)
                began = time.perf_counter()
                baseline = {f['id'] for f in samples if b'UNIQUE_FAILURE' in Path(f['path']).read_bytes()}
                raw_ms = (time.perf_counter() - began) * 1000
                began = time.perf_counter()
                candidates = plan_candidate_files(samples, evidence(client, index, samples, 'UNIQUE_FAILURE'), 'UNIQUE_FAILURE')
                selected = [f for f in samples if f['id'] in candidates]
                actual = {f['id'] for f in selected if b'UNIQUE_FAILURE' in Path(f['path']).read_bytes()}
                indexed_ms = (time.perf_counter() - began) * 1000
                assert actual == baseline == {'sample-7'}
                assert len(selected) == 3  # 完整跨分卷行语义保守保留命中前后相邻卷
                performance = {'files': len(samples), 'candidateFiles': len(selected),
                    'rawBytes': sum(f['bytes'] for f in samples), 'candidateBytes': sum(f['bytes'] for f in selected),
                    'rawMilliseconds': round(raw_ms, 2), 'indexedMilliseconds': round(indexed_ms, 2)}
                cleanup(settings, now() + timedelta(days=1))
                assert evidence(client, index, [file], 'ERROR') == {}
                print(json.dumps({"passed": True, "replay": True, "utf8Boundary": True,
                                  "literalWildcard": True, "missingChunkFallback": True,
                                  "archiveRebuild": True, "retention": True, "lostAckReplay": True, "digestMismatch": True, "synthetic": performance}))
            finally:
                client.delete('/' + index)


if __name__ == '__main__':
    main()
