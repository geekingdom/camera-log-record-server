"""真实归档分页应保持顺序与身份，并避免顺序请求反复扫描已读前缀。"""

import asyncio
import base64
import io
import tarfile
from types import SimpleNamespace

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs.archive_access import read_limiter
from camera_logs.node.files import install_node_routes
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient


def archive_app(tmp_path):
    """同一小时包中登记两个独立分卷，正文使用可校验的确定性内容。"""
    path = tmp_path / "hour.tar.gz"
    payload = bytes(range(256)) * 8192
    with tarfile.open(path, "w:gz", compresslevel=1) as output:
        for name, data in (("first.log", payload), ("second.log", payload[::-1])):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            output.addfile(info, io.BytesIO(data))
    settings = Settings(encryption_key=Fernet.generate_key().decode(), internal_token="probe",
                        log_root=tmp_path, node_id="node")
    repo = Repository(AsyncMongoMockClient().db, settings)
    asyncio.run(repo.db.files.insert_many([
        {"id": name, "path": str(path), "archiveMember": name + ".log", "bytes": len(payload),
         "status": "READY", "nodeId": "node", "sessionId": name} for name in ("first", "second")]))
    app = FastAPI()
    reads = install_node_routes(app, repo, SimpleNamespace(log_root=tmp_path, active={}))
    return app, reads, payload


def page(client, identifier, offset, limit):
    response = client.get(f"/internal/read/{identifier}", params={"offset": offset, "limit": limit},
                          headers={"Authorization": "Bearer probe"})
    response.raise_for_status()
    body = response.json()
    data = base64.b64decode(body["data"])
    assert body["nextOffset"] == offset + len(data)
    assert body["sessionId"] == identifier
    return data


def test_sequential_archive_pages_charge_each_body_byte_once(tmp_path, monkeypatch):
    """以正式路由连续取八页，带宽额度只能包含真正返回的正文，不重复计费前缀。"""
    app, _, payload = archive_app(tmp_path)
    charges = []
    monkeypatch.setattr(read_limiter, "consume", charges.append)
    with TestClient(app) as client:
        received = b"".join(page(client, "first", offset, 262144)
                            for offset in range(0, len(payload), 262144))
    assert received == payload
    assert sum(charges) == len(payload)


def test_interleaved_members_and_overlapping_pages_preserve_exact_bytes(tmp_path):
    """两个成员交错查询及同成员重复区间不能共享可变游标而串读或跳过正文。"""
    app, _, payload = archive_app(tmp_path)
    with TestClient(app) as client:
        for name, offset, limit in [("first", 0, 100), ("second", 0, 130), ("first", 100, 50),
                                    ("first", 0, 80), ("second", 130, 90), ("first", 80, 120)]:
            expected = payload if name == "first" else payload[::-1]
            assert page(client, name, offset, limit) == expected[offset:offset + limit]
