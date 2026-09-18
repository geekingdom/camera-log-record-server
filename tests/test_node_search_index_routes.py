"""节点全文索引证据接口的鉴权、范围和输出边界回归。"""

import asyncio

from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from camera_logs.logs import search_candidates
from camera_logs.node.search_index_routes import install_search_index_routes
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mongomock_motor import AsyncMongoMockClient


def search_index_app(tmp_path, monkeypatch):
    """建立含本地、远端与未封存目录记录的最小节点应用。"""
    settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(),
                        internal_token="node-secret", log_root=tmp_path, node_id="local")
    repo = Repository(AsyncMongoMockClient().db, settings)
    asyncio.run(repo.db.files.insert_many([
        {"id": "local-ready", "nodeId": "local", "status": "READY", "bytes": 42,
         "path": "/never/expose", "indexPath": "/never/expose.idx"},
        {"id": "remote-ready", "nodeId": "remote", "status": "READY", "bytes": 52},
        {"id": "local-open", "nodeId": "local", "status": "OPEN", "bytes": 62},
    ]))
    received = {}

    async def local_index_evidence(actual_repo, files, keyword):
        received["repo"] = actual_repo
        received["files"] = files
        received["keyword"] = keyword
        return {
            "local-ready": {
                "bytes": 42,
                "head": b"head-is-deliberately-long",
                "tail": b"tail-is-deliberately-long",
                "hasNewline": True,
                "mayMatch": False,
                "path": "/must-not-return",
            },
            "remote-ready": {"bytes": 52, "head": b"remote", "tail": b"remote",
                             "hasNewline": True, "mayMatch": True},
        }

    monkeypatch.setattr(search_candidates, "local_index_evidence", local_index_evidence)
    app = FastAPI()
    install_search_index_routes(app, repo)
    return app, repo, received


def test_candidates_authenticates_scopes_files_and_returns_only_bounded_evidence(tmp_path, monkeypatch):
    """接口只将本机 READY 记录交给 helper，且不泄露目录内部字段。"""
    app, repo, received = search_index_app(tmp_path, monkeypatch)
    body = {"fileIds": ["remote-ready", "unknown", "local-ready", "local-open"], "keyword": "needle"}
    with TestClient(app) as client:
        assert client.post("/internal/search-index/candidates", json=body).status_code == 401
        assert client.post("/internal/search-index/candidates", json=body,
                           headers={"Authorization": "Bearer wrong"}).status_code == 401
        response = client.post("/internal/search-index/candidates", json=body,
                               headers={"Authorization": "Bearer node-secret"})

    assert response.status_code == 200
    assert received["repo"] is repo and received["keyword"] == "needle"
    assert [item["id"] for item in received["files"]] == ["local-ready"]
    assert response.json() == {"items": [{
        "fileId": "local-ready", "bytes": 42,
        "headHex": b"head-is-delibera".hex(), "tailHex": b"tail-is-delibera".hex(),
        "hasNewline": True, "mayMatch": False,
    }]}
    serialized = str(response.json())
    assert "path" not in serialized and "indexPath" not in serialized and "must-not-return" not in serialized


def test_candidates_reject_invalid_input_before_querying_helper(tmp_path, monkeypatch):
    """批量数量、标识符和关键词上限均由请求模型拒绝。"""
    app, _repo, received = search_index_app(tmp_path, monkeypatch)
    headers = {"Authorization": "Bearer node-secret"}
    with TestClient(app) as client:
        too_many = client.post("/internal/search-index/candidates", headers=headers,
                               json={"fileIds": [str(index) for index in range(257)], "keyword": "needle"})
        long_id = client.post("/internal/search-index/candidates", headers=headers,
                              json={"fileIds": ["a" * 129], "keyword": "needle"})
        long_keyword = client.post("/internal/search-index/candidates", headers=headers,
                                   json={"fileIds": ["local-ready"], "keyword": "n" * 1025})
        empty = client.post("/internal/search-index/candidates", headers=headers,
                            json={"fileIds": [], "keyword": "needle"})

    assert all(response.status_code == 422 for response in (too_many, long_id, long_keyword, empty))
    assert received == {}
