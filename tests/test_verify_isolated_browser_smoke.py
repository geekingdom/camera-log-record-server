"""验证隔离浏览器冒烟只会连接全部为本机回环地址的 MongoDB。"""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "verify_isolated_browser_smoke.py"
_SPEC = importlib.util.spec_from_file_location("verify_isolated_browser_smoke", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
local_mongo_uri = _MODULE.local_mongo_uri


@pytest.mark.parametrize("uri", [
    "mongodb://127.0.0.1:27017/camera_logs",
    "mongodb://admin:encoded%40password@127.0.0.1:27017/camera_logs?authSource=admin",
    "mongodb://[::1]:27017/camera_logs?directConnection=true",
    "mongodb://localhost:27017,[::1]:27018/replica?replicaSet=rs0",
])
def test_local_mongo_uri_accepts_credentials_and_all_loopback_nodes(uri):
    """认证 URI 和 IPv6 回环副本集不应被隔离验收误判为远程服务。"""
    assert local_mongo_uri(uri)


@pytest.mark.parametrize("uri", [
    "mongodb+srv://cluster.example/camera_logs",
    "mongodb://192.0.2.8:27017/camera_logs",
    "mongodb://127.0.0.1:27017,192.0.2.8:27018/camera_logs",
    "mongodb://localhost:27017,mongo.internal:27018/camera_logs",
    "mongodb:///camera_logs",
    "mongodb://[::1/camera_logs",
])
def test_local_mongo_uri_rejects_srv_remote_mixed_and_invalid_addresses(uri):
    """SRV、远程或混合节点以及无效 URI 都不能用于会创建临时库的验收脚本。"""
    assert not local_mongo_uri(uri)
