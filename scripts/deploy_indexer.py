"""从实际Worker Compose配置派生独立只读索引容器，不修改采集容器或存储。"""

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path


def sidecar_config(config, image, project):
    """复用节点身份/数据库/ES配置，原始日志只读，检查点使用独立持久卷。"""
    worker = config['services']['worker']
    environment = dict(worker.get('environment', {}))
    if not environment.get('ELASTICSEARCH_URL'):
        return None
    environment['ELASTICSEARCH_CHECKPOINT_PATH'] = '/indexer-state/checkpoints.sqlite'
    mounts = [dict(volume, read_only=True) for volume in worker.get('volumes', [])
              if volume.get('target') == '/var/lib/camera-logs']
    if len(mounts) != 1:
        raise ValueError('无法确认Worker日志根挂载，未启动索引器')
    mounts.append({"type": 'volume', "source": 'indexer-state', "target": '/indexer-state'})
    volumes = {'indexer-state': {"name": project + '-indexer-state'}}
    for mount in mounts[:-1]:
        if mount['type'] == 'volume':
            source = mount['source']
            volumes[source] = {"external": True, "name": config['volumes'][source]['name']}
    service = {"image": image, "command": ['python', '-m', 'camera_logs.indexing.runner'],
               "environment": environment, "volumes": mounts, "restart": 'unless-stopped',
               "user": '10001:10001', "mem_limit": '512m', "cpus": 1,
               "logging": {"driver": 'local', "options": {"max-size": '10m', "max-file": '7'}}}
    networks = {}
    if worker.get('network_mode'):
        service['network_mode'] = worker['network_mode']
    else:
        service['networks'] = list(worker.get('networks', {'default': None}))
        for network in service['networks']:
            networks[network] = {"external": True, "name": config['networks'][network]['name']}
    # 新命名卷需要预创建可写目录；只改indexer自己的卷，不改日志卷权限。
    initializer = {"image": image, "user": '0:0', "volumes": [mounts[-1]], "restart": 'no',
                   "command": ['sh', '-c', 'chown 10001:10001 /indexer-state']}
    service['depends_on'] = {"indexer-init": {"condition": 'service_completed_successfully'}}
    return {"services": {"indexer-init": initializer, "indexer": service}, "volumes": volumes, "networks": networks}


def main():
    """仅ES已配置时启动；失败不回滚健康Worker，打印不含凭据的错误。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--env-file', required=True)
    parser.add_argument('--compose-file', required=True)
    parser.add_argument('--project', required=True)
    args = parser.parse_args()
    prefix = ['docker', 'compose', '--env-file', args.env_file, '--project-name', args.project,
              '--file', args.compose_file]
    config = json.loads(subprocess.check_output([*prefix, 'config', '--format', 'json'], text=True))
    if not config.get('services', {}).get('worker', {}).get('environment', {}).get('ELASTICSEARCH_URL'):
        # 显式关闭配置时只停止该派生项目，不删除检查点卷或任何采集容器。
        identifiers = subprocess.check_output(['docker', 'ps', '--quiet', '--filter',
            'label=com.docker.compose.project=' + args.project + '-indexer'], text=True).split()
        if identifiers:
            subprocess.run(['docker', 'stop', '--time', '30', *identifiers], check=True, timeout=60)
        return
    images = subprocess.check_output([*prefix, 'images', '--quiet', 'worker'], text=True).splitlines()
    if len(set(images)) != 1:
        raise ValueError('无法确认已部署Worker镜像')
    data = sidecar_config(config, images[0], args.project)
    # Compose读取完整环境含凭据；文件权限0600且退出即删除，不写部署日志。
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as output:
        json.dump(data, output)
        path = output.name
    try:
        os.chmod(path, 0o600)
        subprocess.run(['docker', 'compose', '--project-name', args.project + '-indexer', '--file', path,
                        'up', '--detach'], check=True, timeout=120)
    finally:
        Path(path).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
