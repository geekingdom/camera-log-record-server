"""以回环 AsyncSSH 和真实 MongoDB 验证 SSH 名额与 socket 生命周期。

本脚本仅启动本机临时 SSH 服务端，使用随机 MongoDB 数据库。它调用生产
``collection.connections.connect``，不连实体设备、不写采集日志；无论断言成功或
失败都会关闭全部客户端、服务端和临时数据库。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from uuid import uuid4

import asyncssh
from camera_logs.collection.connections import connect
from camera_logs.collection.ssh_admission import SshAdmission, SshCapacityError
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


class ServerConnections:
    """保存回环服务端实际 TCP/SSH 会话数量，独立于 Mongo 占位统计。"""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    def opened(self) -> None:
        """记录服务端确认的一个连接，峰值不能超过五。"""
        self.active += 1
        self.peak = max(self.peak, self.active)

    def closed(self) -> None:
        """记录服务端关闭回调，避免负数掩盖客户端重复关闭错误。"""
        self.active -= 1
        if self.active < 0:
            raise AssertionError("回环 SSH 服务端连接计数小于零")


def task(identifier: str, port: int, node_id: str) -> dict[str, Any]:
    """构造两台虚拟 Worker 的最小 SSH 任务身份。"""
    return {"id": identifier, "protocol": "SSH", "ip": "127.0.0.1", "port": port,
            "username": "verify", "password": "verify-password", "runId": f"{identifier}-run",
            "generation": 1, "nodeId": node_id}


async def wait_for(value: callable, expected: int, *, timeout: float = 5) -> None:
    """等待网络关闭回调落地；超时明确给出实际连接数。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if value() == expected:
            return
        await asyncio.sleep(.02)
    raise AssertionError(f"SSH 服务端连接数未收敛 expected={expected} actual={value()}")


async def verify(uri: str) -> dict[str, object]:
    """验证满额拒绝、明确关闭后的第六路重试以及占位完全释放。"""
    database_name = f"ssh_socket_verify_{uuid4().hex}"
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    server = None
    connections: list[Any] = []
    tracker = ServerConnections()
    try:
        hello = await client.admin.command("hello")
        if not hello.get("setName"):
            raise RuntimeError("SSH socket 名额验证需要 MongoDB 副本集")
        settings = Settings(_env_file=None, mongo_uri=uri, database_name=database_name,
                            encryption_key=Fernet.generate_key().decode(), start_background=False)
        repo = Repository(client[database_name], settings)

        class Server(asyncssh.SSHServer):
            """认证成功后保持 shell，直到生产连接路径明确 close。"""

            def connection_made(self, connection: Any) -> None:
                tracker.opened()

            def connection_lost(self, error: Exception | None) -> None:
                tracker.closed()

            def begin_auth(self, username: str) -> bool:
                return username == "verify"

            def password_auth_supported(self) -> bool:
                return True

            def validate_password(self, username: str, password: str) -> bool:
                return username == "verify" and password == "verify-password"

        async def shell(process: Any) -> None:
            """不发送设备输出，只持续保持可由客户端正常关闭的 shell。"""
            await process.stdin.read()

        server = await asyncssh.listen(
            "127.0.0.1", 0, server_factory=Server, process_factory=shell,
            server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")],
        )
        port = server.get_port()
        entries = [task(f"socket-{index}", port, f"worker-{index % 2}") for index in range(6)]
        admissions = [SshAdmission(repo, item) for item in entries]
        attempts = await asyncio.gather(
            *(connect(dict(item) | {"_sshAdmission": admission})
              for item, admission in zip(entries, admissions, strict=True)),
            return_exceptions=True,
        )
        accepted = [value for value in attempts if not isinstance(value, BaseException)]
        rejected = [value for value in attempts if isinstance(value, BaseException)]
        if len(accepted) != 5 or len(rejected) != 1 or not isinstance(rejected[0], SshCapacityError):
            raise AssertionError(f"六路并发 SSH 准入结果错误 attempts={attempts!r}")
        connections.extend(accepted)
        await wait_for(lambda: tracker.active, 5)
        if tracker.peak != 5:
            raise AssertionError(f"实际 SSH shell 峰值应为5，实际为{tracker.peak}")
        slot = await repo.db.ssh_connection_slots.find_one({"_id": "127.0.0.1"})
        if not slot or len(slot.get("claims", [])) != 5:
            raise AssertionError(f"五路 SSH 建连后的名额不正确 slot={slot!r}")

        # 一个关闭收据完成后才允许第六个任务重新申请并建立实际 shell。
        await connections.pop().close()
        await wait_for(lambda: tracker.active, 4)
        sixth = await connect(dict(entries[-1]) | {"_sshAdmission": SshAdmission(repo, entries[-1])})
        connections.append(sixth)
        await wait_for(lambda: tracker.active, 5)

        for connection in connections:
            await connection.close()
        connections.clear()
        await wait_for(lambda: tracker.active, 0)
        released = await repo.db.ssh_connection_slots.find_one({"_id": "127.0.0.1"})
        if not released or released.get("claims") != []:
            raise AssertionError(f"全部 socket 关闭后 Mongo 名额未清空 slot={released!r}")
        return {"passed": True, "temporaryDatabase": database_name, "acceptedShells": 5,
                "rejectedSixth": True, "reopenedAfterClose": True, "finalClaims": 0,
                "serverConnections": tracker.active, "noDeviceAccess": True, "noLogWrites": True}
    finally:
        for connection in connections:
            await connection.close()
        if server is not None:
            server.close()
            await server.wait_closed()
        await client.drop_database(database_name)
        deleted = database_name not in await client.list_database_names()
        await client.close()
        if not deleted:
            raise AssertionError("SSH socket 验证临时数据库没有删除")


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify(Settings().mongo_uri)), ensure_ascii=False, sort_keys=True))
