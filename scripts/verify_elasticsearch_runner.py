"""真实 ES/Mongo 验收旁路索引器的归档分组、目录索引和删除补偿。"""

import asyncio
import hashlib
import io
import json
import os
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.indexing.client import checked, connection, evidence
from camera_logs.indexing.runner import run
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


def _archive(path: Path) -> tuple[list[dict], dict[str, bytes]]:
    """创建物理顺序与随机目录 ID 不同的共享小时包，覆盖跨分页分组语义。"""
    contents = {f'part-{number:03d}.log': f'line {number}\n'.encode() for number in range(49)}
    with tarfile.open(path, 'w:gz') as output:
        for name in reversed(list(contents)):
            member = tarfile.TarInfo(name)
            member.size = len(contents[name])
            output.addfile(member, io.BytesIO(contents[name]))
    hour = datetime.now(UTC).replace(minute=0, second=0, microsecond=0).isoformat()
    files = []
    for number, (name, data) in enumerate(contents.items()):
        files.append({
            'id': f'{(number * 37) % 49:03d}', 'taskId': 'runner-task', 'nodeId': 'runner-node',
            'runId': 'run', 'sessionId': 'session', 'hour': hour, 'path': str(path),
            'archiveMember': name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'status': 'READY',
        })
    return files, contents


async def _main() -> None:
    """用随机库和索引完整运行两轮回放及一次 DELETED 清理，finally 无条件回收。"""
    index = 'camera-runner-verify-' + uuid4().hex
    database_name = 'camera_runner_verify_' + uuid4().hex
    mongo_uri = os.getenv('VERIFY_MONGO_URI', 'mongodb://127.0.0.1:27019/?directConnection=true')
    es_url = os.getenv('VERIFY_ES_URL', 'http://127.0.0.1:19200')
    async with AsyncMongoClient(mongo_uri, tz_aware=True, serverSelectionTimeoutMS=5000) as mongo:
        database = mongo[database_name]
        with tempfile.TemporaryDirectory(prefix='camera-es-runner-') as directory:
            root = Path(directory)
            settings = Settings(
                mongo_uri=mongo_uri, database_name=database_name, log_root=root, node_id='runner-node',
                encryption_key=Fernet.generate_key().decode(), bootstrap_token='runner-verify',
                elasticsearch_url=es_url, elasticsearch_index=index,
                elasticsearch_checkpoint_path=root / 'checkpoints.sqlite', elasticsearch_batch_chunks=1,
                elasticsearch_file_pause_seconds=0, elasticsearch_retention_days=7,
            )
            repo = Repository(database, settings)
            await repo.initialize()
            archive = root / 'shared.tar.gz'
            files, _contents = _archive(archive)
            await database.tasks.insert_one({'id': 'runner-task', 'resourceId': 'runner-resource'})
            await database.files.insert_many(files)
            explain = await database.command('explain', {
                'find': 'files', 'filter': {'nodeId': 'runner-node', 'path': str(archive), 'status': 'READY'},
                'sort': {'id': 1},
            }, verbosity='executionStats')
            plan = json.dumps(explain['queryPlanner']['winningPlan'])
            stats = explain['executionStats']
            assert 'files_node_archive_members' in plan
            assert stats['totalKeysExamined'] == stats['totalDocsExamined'] == len(files)
            opens = 0
            original_open = tarfile.open

            def count_open(*args, **kwargs):
                nonlocal opens
                opens += 1
                return original_open(*args, **kwargs)

            try:
                with patch('camera_logs.indexing.archive.tarfile.open', count_open):
                    await run(settings, once=True)
                    first_opens = opens
                    assert first_opens == 1
                    with connection(settings) as client:
                        proof = evidence(client, index, files)
                        assert set(proof) == {file['id'] for file in files}
                        result = checked(client.post(f'/{index}/_search', json={
                            'size': 1, 'query': {'term': {'kind': 'chunk'}},
                        }))['hits']['hits'][0]['_source']
                        assert result['resourceId'] == 'runner-resource'
                    await run(settings, once=True)
                    assert opens == first_opens
                    await database.files.update_many({}, {'$set': {'status': 'DELETED'}})
                    await run(settings, once=True)
                    with connection(settings) as client:
                        assert evidence(client, index, files) == {}
                print(json.dumps({
                    'passed': True, 'members': len(files), 'archiveOpens': first_opens,
                    'secondRoundNoOpen': True, 'deletedSourcesPruned': True,
                    'resourceId': 'runner-resource',
                    'explain': {'keysExamined': stats['totalKeysExamined'], 'docsExamined': stats['totalDocsExamined']},
                }))
            finally:
                with connection(settings) as client:
                    client.delete('/' + index)
                await mongo.drop_database(database_name)


if __name__ == '__main__':
    asyncio.run(_main())
