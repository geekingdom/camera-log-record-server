"""真实本地 AsyncSSH 的暂停、恢复与同运行预算回归。"""

import asyncio

import asyncssh
import pytest
from camera_logs.collection.connections import _connect_ssh
from camera_logs.collection.runtime import SessionRuntime
from camera_logs.collection.ssh_admission import SshAdmission
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.node.worker import Worker
from camera_logs.tasks.scheduler import schedule_once
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

pytestmark = pytest.mark.usefixtures("mock_reservation_transaction", "mock_claim_transaction")
# MongoMock 仅验证暂停后的预算、锁和 admission 语义；真实 Mongo 事务原子性由独立验证覆盖。


def _command_lines(payload: bytearray) -> list[bytes]:
    """按 SSH shell 的换行边界还原服务端实际收到的命令。"""
    return [line.rstrip(b"\r") for line in bytes(payload).split(b"\n")[:-1]]


async def test_ssh_pause_closes_server_session_and_resume_reuses_run_budget(tmp_path):
    """暂停确认真实 EOF；同一运行恢复新会话，最终没有 SSH 名额残留。"""
    authentication, payloads, handlers, server_closed, process_eofs = [], [], set(), set(), set()
    connection_number = 0
    runtimes = []

    class Server(asyncssh.SSHServer):
        """本地服务端记录认证、传输关闭及 shell stdin，不向客户端输出正文。"""

        def connection_made(self, _connection):
            nonlocal connection_number
            connection_number += 1
            self.number = connection_number

        def begin_auth(self, _username):
            return True

        def password_auth_supported(self):
            return True

        def validate_password(self, username, password):
            authentication.append((username, password))
            return username == "collector" and password == "password"

        def connection_lost(self, _error):
            server_closed.add(self.number)

    async def silent_process(process):
        """只读取真实 SSH stdin；EOF 表示客户端 shell 已经回收。"""
        handler = asyncio.current_task()
        assert handler is not None
        handlers.add(handler)
        number = len(payloads) + 1
        payload = bytearray()
        payloads.append(payload)
        try:
            while chunk := await process.stdin.read(1):
                payload.extend(chunk.encode() if isinstance(chunk, str) else chunk)
        finally:
            process_eofs.add(number)
            handlers.discard(handler)

    async def until(predicate):
        """避免以固定睡眠判断服务端命令、EOF 或关闭回调的异步顺序。"""
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    server = await asyncssh.listen(
        "127.0.0.1", 0, server_factory=Server, process_factory=silent_process,
        server_host_keys=[asyncssh.generate_private_key("ssh-ed25519")], line_editor=False,
    )
    try:
        settings = Settings(_env_file=None, encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
                            node_id="ssh-pause", start_background=False)
        repo = Repository(AsyncMongoMockClient().camera_logs, settings)
        await repo.initialize()
        task = {
            "id": "ssh-pause", "runId": "same-run", "nodeId": settings.node_id, "generation": 1,
            "status": "PENDING", "desiredState": "RUNNING", "protocol": "SSH", "ip": "127.0.0.1",
            "port": server.get_port(), "username": "collector", "passwordEncrypted": repo.encrypt("password"),
            "storageIdentity": "ssh-pause", "initialCommands": [
                {"command": "outputClose"}, {"command": "outputOpen"},
                {"command": "setDebug -m all -l 7 -d 111"}, {"command": "prtHardInfo"},
            ],
            "scheduledCommands": [{"id": "periodic", "command": "probe", "totalExecutions": 2,
                                   "intervalSeconds": 1}],
        }
        await repo.db.tasks.insert_one(task.copy())
        await repo.db.runs.insert_one({"id": "same-run", "taskId": task["id"], "nodeId": settings.node_id,
                                       "endedAt": None})
        await repo.db.endpoint_locks.insert_one({"taskId": task["id"], "runId": "same-run",
                                                 "endpoint": f"127.0.0.1:{server.get_port()}"})
        await repo.db.nodes.insert_one({"id": settings.node_id, "heartbeat": now(), "capacity": 100,
                                        "diskPercent": 1, "accepting": True})

        async def connect(config):
            """每个真实会话独立申请 Mongo SSH 名额，关闭后才能允许下一次申请。"""
            admission = SshAdmission(repo, config)
            return await _connect_ssh(dict(config) | {"_sshAdmission": admission}, config["ip"], config["port"])

        runtime = SessionRuntime(repo, task, connection_factory=connect)
        runtimes.append(runtime)
        await until(lambda: payloads and b"probe" in _command_lines(payloads[0]))
        first_session = runtime.collector.session_id
        assert await repo.db.ssh_connection_slots.count_documents({"claims.taskId": task["id"]}) == 1
        worker = Worker(repo)
        worker.active[task["id"]] = runtime

        await repo.db.tasks.update_one({"id": task["id"]}, {"$set": {"desiredState": "PAUSED"}})
        await worker.pause(runtime)
        await until(lambda: process_eofs == {1} and server_closed == {1})

        paused = await repo.get("tasks", task["id"])
        assert runtime.background.done()
        assert task["id"] not in worker.active
        assert (paused["status"], paused["desiredState"], paused["runId"], paused["nodeId"]) == (
            "PAUSED", "PAUSED", "same-run", None,
        )
        assert (await repo.db.runs.find_one({"id": "same-run"}))["endedAt"] is None
        assert await repo.db.endpoint_locks.find_one({"taskId": task["id"], "runId": "same-run"})
        assert (await repo.db.budgets.find_one({"_id": "same-run:periodic"}))["attempts"] == 1
        assert await repo.db.ssh_connection_slots.count_documents({"claims.taskId": task["id"]}) == 0
        # 调度周期不得重新认领 PAUSED；服务器也不应观察到第二会话。
        await schedule_once(repo)
        assert (await repo.get("tasks", task["id"]))["status"] == "PAUSED"
        assert (await repo.get("tasks", task["id"]))["nodeId"] is None
        assert len(payloads) == 1 and server_closed == {1}

        await repo.db.tasks.update_one({"id": task["id"]}, {"$set": {"desiredState": "RUNNING"}})
        await schedule_once(repo)
        resumed = await repo.get("tasks", task["id"])
        assert (resumed["status"], resumed["desiredState"], resumed["runId"]) == ("PENDING", "RUNNING", "same-run")

        runtime = SessionRuntime(repo, resumed, connection_factory=connect)
        runtimes.append(runtime)
        await until(lambda: len(payloads) == 2 and b"probe" in _command_lines(payloads[1]))
        second_session = runtime.collector.session_id
        assert await repo.db.ssh_connection_slots.count_documents({"claims.taskId": task["id"]}) == 1
        await runtime.stop()
        await until(lambda: process_eofs == {1, 2} and server_closed == {1, 2})

        commands = [item async for item in repo.db.commands.find({"kind": "SCHEDULED"})]
        assert authentication == [("collector", "password"), ("collector", "password")]
        assert [_command_lines(payload) for payload in payloads] == [
            [b"outputClose", b"outputOpen", b"setDebug -m all -l 7 -d 111", b"prtHardInfo", b"probe"],
            [b"outputClose", b"outputOpen", b"setDebug -m all -l 7 -d 111", b"prtHardInfo", b"probe"],
        ]
        assert first_session != second_session
        assert (await repo.db.budgets.find_one({"_id": "same-run:periodic"}))["attempts"] == 2
        assert len({item["sessionId"] for item in commands}) == 2
        assert {item["status"] for item in commands} == {"SENT"}
        assert await repo.db.ssh_connection_slots.count_documents({"claims.taskId": task["id"]}) == 0
    finally:
        pending = list(handlers)
        try:
            for item in runtimes:
                await item.stop()
        finally:
            server.close()
            try:
                await server.wait_closed()
            finally:
                for handler in pending:
                    handler.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
