"""在随机本地数据库中运行 API、Worker 与浏览器冒烟，绝不触碰现有服务。"""

import asyncio
import ipaddress
import json
import os
import secrets
import socket
import tempfile
from pathlib import Path
from uuid import uuid4

import httpx
import telnetlib3
from camera_logs.common.config import Settings
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient
from pymongo.uri_parser import parse_uri

ROOT = Path(__file__).resolve().parents[1]


def local_mongo_uri(uri: str) -> bool:
    """只接受全部节点均为回环地址的普通 MongoDB URI。

    隔离浏览器验收会创建并删除随机数据库，因此凭据、端口和 IPv6 表达形式不能影响
    本机判断；SRV 记录可能在解析时扩展到远程主机，始终拒绝。
    """
    if uri.startswith("mongodb+srv://"):
        return False
    try:
        nodes = parse_uri(uri)["nodelist"]
    except Exception:  # noqa: BLE001 - 无法可靠解析时必须拒绝访问配置数据库。
        return False
    if not nodes:
        return False
    for host, _port in nodes:
        if host.lower() == "localhost":
            continue
        try:
            if not ipaddress.ip_address(host).is_loopback:
                return False
        except ValueError:
            return False
    return True


def free_port():
    """向内核申请独立回环端口，子进程使用 strictPort 防止误连已有服务。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def stop(process):
    """只回收本脚本持有的子进程，超时才强制结束。"""
    if process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), 10)
    except TimeoutError:
        process.kill()
        await process.wait()


async def wait_ready(url, processes):
    """等待隔离服务健康，同时及时暴露 API、Worker 或 Vite 的异常退出。"""
    async with httpx.AsyncClient(timeout=1) as client, asyncio.timeout(45):
        while True:
            if any(process.returncode is not None for process in processes):
                raise RuntimeError("隔离 API、Worker 或 Vite 服务意外退出")
            try:
                if (await client.get(url)).is_success:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.2)


def diagnostic_tail(path):
    """失败时仅输出临时子进程日志末尾，便于定位隔离链路的组件边界。"""
    if not path.is_file():
        return ""
    return path.read_text(errors="replace")[-8000:]


class SerialSource:
    """持续输出带换行的本地 Telnet 字节流，使 Worker 产生真实实时帧和小时归档。"""

    def __init__(self):
        self.server = None
        self.port = 0
        self.connections = set()

    async def start(self):
        """监听随机本地端口；模拟源不接受或访问任何外部设备。"""
        self.server = await telnetlib3.create_server(
            host="127.0.0.1", port=0, shell=self._serve, encoding=False,
            connect_maxwait=.05, timeout=False,
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def _serve(self, reader, writer):
        task = asyncio.current_task()
        self.connections.add(task)

        async def emit():
            number = 0
            while True:
                writer.write(f"isolated-browser-smoke seq={number:06d}\n".encode())
                await writer.drain()
                number += 1
                await asyncio.sleep(.08)

        emitter = asyncio.create_task(emit())
        try:
            while await reader.read(4096):
                pass
        finally:
            emitter.cancel()
            await asyncio.gather(emitter, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            self.connections.discard(task)

    async def close(self):
        """关闭模拟监听与所有连接处理协程。"""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)


def environment(database, mongo_uri, log_root, api_port, node_port, vite_port, screenshots):
    """生成仅供本轮随机库使用的进程环境，密钥与管理员口令不来自真实环境。"""
    return os.environ | {
        "MONGO_URI": mongo_uri,
        "DATABASE_NAME": database,
        "LOG_ROOT": str(log_root),
        "BOOTSTRAP_TOKEN": secrets.token_urlsafe(32),
        "INTERNAL_TOKEN": secrets.token_urlsafe(32),
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ADMIN_USERNAME": "isolated-browser-admin",
        "ADMIN_PASSWORD": secrets.token_urlsafe(24),
        # 隔离 API 仍需运行调度器，才能把合成任务分配给同一随机库中的 Worker。
        "START_BACKGROUND": "true",
        "NODE_ID": f"isolated-browser-node-{uuid4().hex[:8]}",
        "NODE_URL": f"http://127.0.0.1:{node_port}",
        "NODE_BIND_IP": "127.0.0.1",
        "NODE_PORT": str(node_port),
        "BROWSER_BASE_URL": f"http://127.0.0.1:{vite_port}",
        "BROWSER_SCREENSHOTS": str(screenshots),
        "BROWSER_SMOKE_CREATE_SYNTHETIC_TASK": "true",
        "PLAYWRIGHT_MODULE": os.getenv("PLAYWRIGHT_MODULE", "playwright"),
    }


async def main():
    """执行隔离的真实 Worker、WebSocket、小时下载和浏览器 UI 验收。"""
    configured = Settings()
    if not local_mongo_uri(configured.mongo_uri):
        raise RuntimeError("隔离浏览器验收仅允许本机 MongoDB")
    database = f"isolated_browser_smoke_{uuid4().hex}"
    if database == configured.database_name:
        raise RuntimeError("随机验收数据库不能等于当前数据库")
    api_port, node_port, vite_port = free_port(), free_port(), free_port()
    processes, source = [], SerialSource()
    process_logs = []
    failed = True
    mongo = AsyncMongoClient(configured.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    with tempfile.TemporaryDirectory(prefix="camera-isolated-browser-") as temporary:
        temporary_path = Path(temporary)
        env = environment(database, configured.mongo_uri, temporary_path / "logs", api_port, node_port, vite_port,
                          temporary_path / "screenshots")
        env["BROWSER_SMOKE_SYNTHETIC_HOST"] = "127.0.0.1"
        try:
            await source.start()
            env["BROWSER_SMOKE_SYNTHETIC_PORT"] = str(source.port)
            api_log = (temporary_path / "api.log").open("w")
            process_logs.append(("api", api_log))
            api = await asyncio.create_subprocess_exec(
                ROOT / ".venv/bin/python", "-m", "uvicorn", "camera_logs.main:app", "--host", "127.0.0.1",
                "--port", str(api_port), cwd=ROOT, env=env, stdout=api_log, stderr=api_log,
            )
            processes.append(api)
            worker_log = (temporary_path / "worker.log").open("w")
            process_logs.append(("worker", worker_log))
            worker = await asyncio.create_subprocess_exec(
                ROOT / ".venv/bin/python", "-m", "camera_logs.worker", cwd=ROOT, env=env,
                stdout=worker_log, stderr=worker_log,
            )
            processes.append(worker)
            vite_code = (
                "import {createServer} from 'vite';"
                f"const server=await createServer({{server:{{host:'127.0.0.1',port:{vite_port},strictPort:true,proxy:{{'/api':{{target:'http://127.0.0.1:{api_port}',ws:true}}}}}}}});"
                "await server.listen();"
            )
            vite = await asyncio.create_subprocess_exec(
                "node", "--input-type=module", "-e", vite_code, cwd=ROOT / "frontend", env=env,
                stdout=(vite_log := (temporary_path / "vite.log").open("w")), stderr=vite_log,
            )
            process_logs.append(("vite", vite_log))
            processes.append(vite)
            await wait_ready(f"http://127.0.0.1:{api_port}/health", processes)
            await wait_ready(f"http://127.0.0.1:{node_port}/health", processes)
            await wait_ready(env["BROWSER_BASE_URL"], processes)
            browser = await asyncio.create_subprocess_exec("node", "scripts/browser_smoke.mjs", cwd=ROOT, env=env)
            processes.append(browser)
            async with asyncio.timeout(240):
                if await browser.wait() != 0:
                    raise RuntimeError("隔离 browser_smoke.mjs 失败")
            expected = ("live-desktop.png", "archives-mobile.png", "logs-workspace-desktop.png", "archives-workspace-mobile.png")
            screenshots = [temporary_path / "screenshots" / name for name in expected]
            if any(not path.is_file() or path.stat().st_size == 0 for path in screenshots):
                raise RuntimeError("隔离浏览器验收截图不完整")
            print(json.dumps({"passed": True, "isolatedDatabase": database, "realWorker": True,
                              "realWebSocket": True, "browserHourDownload": True, "screenshots": list(expected)}, ensure_ascii=False))
            failed = False
        finally:
            for process in reversed(processes):
                await stop(process)
            await source.close()
            for _, log in process_logs:
                log.close()
            if failed:
                for name, log in process_logs:
                    tail = diagnostic_tail(Path(log.name))
                    if tail:
                        print(f"--- isolated {name} log tail ---\n{tail}")
            try:
                await mongo.drop_database(database)
            finally:
                await mongo.close()
    print(json.dumps({"temporaryDatabaseDropped": True, "temporaryLogDirectoryDropped": True}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
