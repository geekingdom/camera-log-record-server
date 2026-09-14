"""在可销毁的随机 Mongo 库中验收审计排障界面的真实 Cookie 会话和三类事件。"""

import asyncio
import json
import os
import secrets
import socket
import tempfile
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from camera_logs.common.database import now
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient

ROOT = Path(__file__).resolve().parents[1]
SCREENSHOT_ROOT = ROOT / "output" / "playwright"
PLAYWRIGHT = os.getenv("PLAYWRIGHT_MODULE", "playwright")


def free_port() -> int:
    """向系统申请空闲本机端口，子进程以 strictPort 避免误连现有开发服务。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def isolated_environment(database: str, mongo_uri: str, logs: Path, api_port: int, vite_port: int, screenshots: Path) -> dict[str, str]:
    """覆盖所有会话、加密和存储配置；不读取或输出真实管理员密码。"""
    password = secrets.token_urlsafe(24)
    return os.environ | {
        "MONGO_URI": mongo_uri,
        "DATABASE_NAME": database,
        "LOG_ROOT": str(logs),
        "BOOTSTRAP_TOKEN": secrets.token_urlsafe(32),
        "INTERNAL_TOKEN": secrets.token_urlsafe(32),
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "START_BACKGROUND": "false",
        "ADMIN_USERNAME": "audit-browser-admin",
        "ADMIN_PASSWORD": password,
        "BROWSER_BASE_URL": f"http://127.0.0.1:{vite_port}",
        "BROWSER_ISOLATED_AUTH": "true",
        "BROWSER_ADMIN_USERNAME": "audit-browser-admin",
        "BROWSER_ADMIN_PASSWORD": password,
        "BROWSER_SCREENSHOTS": str(screenshots),
        "PLAYWRIGHT_MODULE": PLAYWRIGHT,
        "AUDIT_BROWSER_API_PORT": str(api_port),
    }


async def wait_ready(url: str, processes: list[asyncio.subprocess.Process]) -> None:
    """等待临时服务响应，同时及时暴露任一子进程异常退出。"""
    async with httpx.AsyncClient(timeout=1) as client, asyncio.timeout(45):
        while True:
            if any(process.returncode is not None for process in processes):
                raise RuntimeError("隔离 API 或 Vite 服务意外退出")
            try:
                if (await client.get(url)).is_success:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)


async def stop(process: asyncio.subprocess.Process) -> None:
    """先温和终止临时子进程，超时才强制回收，绝不操作工作区已有进程。"""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=10)
    except TimeoutError:
        process.kill()
        await process.wait()


async def main() -> None:
    """启动隔离服务、准备无设备夹具、运行浏览器并回收所有临时资源。"""
    configured = Settings()
    mongo_address = urlsplit(configured.mongo_uri)
    if mongo_address.scheme != "mongodb" or mongo_address.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("浏览器验收仅允许本机 MongoDB，拒绝配置的远程数据库")
    database = f"audit_browser_{uuid4().hex}"
    if database == configured.database_name:
        raise RuntimeError("随机验收数据库不能等于配置数据库")
    api_port, vite_port = free_port(), free_port()
    screenshots = SCREENSHOT_ROOT / f"audit-browser-{uuid4().hex[:12]}"
    processes: list[asyncio.subprocess.Process] = []
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    with tempfile.TemporaryDirectory(prefix="camera-audit-browser-") as temporary:
        environment = isolated_environment(database, configured.mongo_uri, Path(temporary) / "logs", api_port, vite_port, screenshots)
        try:
            api = await asyncio.create_subprocess_exec(
                ROOT / ".venv/bin/python", "-m", "uvicorn", "camera_logs.main:app", "--host", "127.0.0.1", "--port", str(api_port),
                cwd=ROOT, env=environment, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            processes.append(api)
            await wait_ready(f"http://127.0.0.1:{api_port}/health", processes)
            await mongo[database].tasks.insert_one({
                "id": "audit-browser-task", "name": "隔离连接演示任务", "ip": "198.51.100.77",
            })
            await mongo[database].events.insert_one({
                "type": "CONNECTION_GAP", "taskId": "audit-browser-task", "nodeId": "isolated-browser",
                "createdAt": now(), "reason": "隔离验收模拟：连续十秒未收到输出，已请求重新连接",
            })
            await mongo[database].audit.insert_many([
                {"action": "cursor-browser", "actor": "system", "createdAt": now() - timedelta(seconds=index + 1),
                 "targetId": f"isolated-event-{index}"}
                for index in range(55)
            ])
            vite_code = (
                "import {createServer} from 'vite';"
                f"const server=await createServer({{server:{{host:'127.0.0.1',port:{vite_port},strictPort:true,proxy:{{'/api':{{target:'http://127.0.0.1:{api_port}',ws:true}}}}}}}});"
                "await server.listen();"
            )
            vite = await asyncio.create_subprocess_exec(
                "node", "--input-type=module", "-e", vite_code, cwd=ROOT / "frontend", env=environment,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            processes.append(vite)
            await wait_ready(environment["BROWSER_BASE_URL"], processes)
            browser = await asyncio.create_subprocess_exec("node", "scripts/browser_audit_workspace.mjs", cwd=ROOT, env=environment)
            processes.append(browser)
            async with asyncio.timeout(120):
                if await browser.wait() != 0:
                    raise RuntimeError("审计浏览器验收失败")
            expected = [screenshots / name for name in (
                "audit-workspace-desktop.png", "audit-workspace-sidebar-collapsed.png", "audit-workspace-mobile.png",
                "audit-workspace-runtime-table.png", "audit-workspace-runtime-detail.png",
            )]
            if any(not path.is_file() or path.stat().st_size == 0 for path in expected):
                raise RuntimeError("浏览器截图不完整")
            print(json.dumps({"passed": True, "cookieOnly": True, "tabs": 3, "requestConflict409": True,
                              "runtimeFixtureWithoutDevice": True, "screenshots": [str(path) for path in expected]}, ensure_ascii=False))
        finally:
            for process in reversed(processes):
                await stop(process)
            try:
                await mongo.drop_database(database)
            finally:
                await mongo.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryLogDirectoryDropped": True}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
