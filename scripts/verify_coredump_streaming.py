"""隔离 API/节点验证大文件流式下载，所有数据在随机库与临时目录中自动清理。"""

import argparse
import asyncio
import hashlib
import json
import resource
import socket
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

import httpx
import uvicorn
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.main import create_app
from camera_logs.node.files import install_node_routes
from cryptography.fernet import Fernet
from fastapi import FastAPI
from pymongo import AsyncMongoClient


@asynccontextmanager
async def serve(app):
    """显式绑定空闲环回端口，退出时等待服务器和连接收尾。"""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", timeout_graceful_shutdown=10))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(500):
            if server.started:
                break
            if task.done():
                await task
                raise RuntimeError("验证服务未启动")
            await asyncio.sleep(.02)
        if not server.started:
            raise TimeoutError("验证服务启动超时")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
        listener.close()


def zero_digest(size):
    """固定一 MiB 缓冲计算稀疏零文件摘要，不加载大文件。"""
    chunk = bytes(1024 * 1024)
    digest = hashlib.sha256()
    while size:
        count = min(size, len(chunk))
        digest.update(chunk[:count])
        size -= count
    return digest.hexdigest()


async def main(size, readers):
    """流过真实 TCP 的 API->Worker 链路，校验全量和并发局部内容。"""
    configured, database_name = Settings(), "coredump_stream_verify_" + uuid4().hex
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True)
    with TemporaryDirectory(prefix="camera-core-stream-") as temporary:
        root = Path(temporary)
        settings = Settings(_env_file=None, mongo_uri=configured.mongo_uri, database_name=database_name,
                            log_root=root / "logs", node_id="verify-node", internal_token=uuid4().hex,
                            encryption_key=Fernet.generate_key().decode(), bootstrap_token=uuid4().hex,
                            start_background=False)
        try:
            repo = Repository(mongo[database_name], settings)
            await repo.initialize()
            output = settings.log_root / "exports" / "coredumps" / "verify"
            output.mkdir(parents=True)
            path = output / "large.core"
            with path.open("wb") as stream:
                stream.truncate(size)
            digest = await asyncio.to_thread(zero_digest, size)
            await repo.db.coredump_exports.insert_one({"id": "verify", "actor": "bootstrap", "status": "SUCCEEDED",
                "coordinatorNodeId": settings.node_id, "resultPath": str(path), "filename": "large.core",
                "bytes": size, "etag": f'"{digest}"', "expiresAt": now() + timedelta(hours=1)})
            node = FastAPI()
            install_node_routes(node, repo, SimpleNamespace(log_root=settings.log_root, active={}))
            async with serve(node) as node_url:
                await repo.db.nodes.insert_one({"id": settings.node_id, "url": node_url})
                async with serve(create_app(settings, db=mongo[database_name])) as api_url:
                    endpoint = api_url + "/api/v1/coredump-exports/verify/content"
                    async with httpx.AsyncClient(timeout=300, headers={"Authorization": "Bearer " + settings.bootstrap_token}) as client:
                        async def download(headers, expected_size, expected_digest, status):
                            actual, total = hashlib.sha256(), 0
                            async with client.stream("GET", endpoint, headers=headers) as response:
                                assert response.status_code == status, response.status_code
                                async for chunk in response.aiter_bytes(262144):
                                    total += len(chunk)
                                    actual.update(chunk)
                            assert total == expected_size, (total, expected_size)
                            assert actual.hexdigest() == expected_digest
                        await asyncio.gather(*(download({}, size, digest, 200) for _ in range(readers)))
                        count = min(size, 1048576)
                        small_digest = zero_digest(count)
                        await asyncio.gather(*(download({"Range": f"bytes=-{count}", "If-Range": f'"{digest}"'},
                                                       count, small_digest, 206) for _ in range(8)))
                        invalid = await client.get(endpoint, headers={"Range": f"bytes={size}-"})
                        assert invalid.status_code == 416
                        assert invalid.headers.get("content-range") == f"bytes */{size}"
                    # 去掉 Bearer 后仅凭路径受限的下载 Cookie 下载尾部。
                    async with httpx.AsyncClient(timeout=30) as browser:
                        ticket = await browser.post(api_url + "/api/v1/coredump-exports/verify/browser-session",
                            headers={"Authorization": "Bearer " + settings.bootstrap_token})
                        assert ticket.status_code == 200, ticket.text
                        response = await browser.get(api_url + ticket.json()["url"], headers={"Range": "bytes=-16"})
                        assert response.status_code == 206 and response.content == bytes(16)
            print(json.dumps({"passed": True, "fileBytes": size, "fullConcurrentReaders": readers,
                              "rangeConcurrentReaders": 8, "nativeCookieDownload": True,
                              "maxRssPlatformUnits": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                              "scope": "local HTTP streaming, not NFS reception or production capacity"}))
        finally:
            await mongo.drop_database(database_name)
            await mongo.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryDirectoryRemoved": True}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mib", type=int, default=2048)
    parser.add_argument("--readers", type=int, default=2)
    arguments = parser.parse_args()
    if arguments.mib < 1 or not 1 <= arguments.readers <= 8:
        parser.error("mib须为正数，readers范围1至8")
    asyncio.run(main(arguments.mib * 1024 * 1024, arguments.readers))
