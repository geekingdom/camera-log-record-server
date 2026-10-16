"""独立索引部署保持采集日志只读、节点网络一致和独立可写检查点。"""

import runpy
from pathlib import Path

import pytest

sidecar_config = runpy.run_path(str(Path(__file__).parents[1] / "scripts/deploy_indexer.py"))["sidecar_config"]


def worker_config():
    """最小真实Compose解析形状，不包含生产凭据。"""
    return {'services': {'worker': {'environment': {'ELASTICSEARCH_URL': 'https://es:9200', 'NODE_ID': 'a'},
            'volumes': [{'type': 'bind', 'source': '/logs', 'target': '/var/lib/camera-logs'}],
            'network_mode': 'host'}}}


def test_sidecar_is_readonly_and_uses_independent_checkpoint_volume():
    result = sidecar_config(worker_config(), 'sha256:image', 'platform-worker')
    service = result['services']['indexer']
    assert service['network_mode'] == 'host'
    assert service['volumes'][0]['read_only'] is True
    assert service['volumes'][1]['source'] == 'indexer-state'
    assert result['services']['indexer-init']['volumes'] == [service['volumes'][1]]
    assert result['volumes']['indexer-state']['name'] == 'platform-worker-indexer-state'


def test_missing_log_mount_refuses_guess_and_disabled_stays_optional():
    config = worker_config()
    config['services']['worker']['volumes'] = []
    with pytest.raises(ValueError, match='日志根'):
        sidecar_config(config, 'image', 'project')
    config['services']['worker']['environment']['ELASTICSEARCH_URL'] = ''
    assert sidecar_config(config, 'image', 'project') is None


def test_named_volume_and_network_reference_existing_compose_resources():
    config = worker_config()
    worker = config['services']['worker']
    worker.pop('network_mode')
    worker['networks'] = {'default': None}
    worker['volumes'][0] = {'type': 'volume', 'source': 'worker-data', 'target': '/var/lib/camera-logs'}
    config['networks'] = {'default': {'name': 'existing-default'}}
    config['volumes'] = {'worker-data': {'name': 'existing-data'}}
    result = sidecar_config(config, 'image', 'project')
    assert result['networks']['default'] == {'external': True, 'name': 'existing-default'}
    assert result['volumes']['worker-data'] == {'external': True, 'name': 'existing-data'}
