"""验证跨 Worker SSH 五连接准入的原子占位、精确释放和保守未知结果。"""

import asyncio
from types import SimpleNamespace

import pytest
from camera_logs.collection.ssh_admission import (
    SshAdmission,
    SshCapacityError,
    SshSlotUncertain,
    normalize_ssh_address,
    release_task_slots,
)
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient
from pymongo.errors import AutoReconnect


def _task(index: int, *, ip: str = "192.0.2.9", port: int = 22, generation: int = 1) -> dict:
    return {"id": f"task-{index}", "runId": f"run-{index}", "generation": generation,
            "nodeId": f"node-{index}", "ip": ip, "port": port, "protocol": "SSH"}


async def _repo():
    repo = Repository(AsyncMongoMockClient().db, Settings(_env_file=None, encryption_key=Fernet.generate_key().decode()))
    await repo.initialize()
    return repo


@pytest.mark.asyncio
async def test_same_ip_different_ports_share_exactly_five_ssh_slots():
    repo = await _repo()
    admissions = [SshAdmission(repo, _task(index, port=22 + index)) for index in range(6)]
    tokens = await asyncio.gather(*(item.acquire() for item in admissions[:5]))
    assert len(set(tokens)) == 5
    with pytest.raises(SshCapacityError, match="名额已满"):
        await admissions[5].acquire()
    slot = await repo.db.ssh_connection_slots.find_one({"_id": "192.0.2.9"})
    assert len(slot["claims"]) == 5


@pytest.mark.asyncio
async def test_release_is_idempotent_and_cannot_release_another_worker_token():
    repo = await _repo()
    first, second = SshAdmission(repo, _task(1)), SshAdmission(repo, _task(2))
    await first.acquire()
    await second.acquire()
    assert await first.release() is True
    assert await first.release() is False
    slot = await repo.db.ssh_connection_slots.find_one({"_id": "192.0.2.9"})
    assert [claim["taskId"] for claim in slot["claims"]] == ["task-2"]


@pytest.mark.asyncio
async def test_explicit_task_release_requires_exact_run_and_generation():
    repo = await _repo()
    task = _task(1)
    admission = SshAdmission(repo, task)
    await admission.acquire()
    assert await release_task_slots(repo, task | {"generation": 2}) == 0
    assert await repo.db.ssh_connection_slots.find_one({"_id": "192.0.2.9", "claims.taskId": "task-1"})
    assert await release_task_slots(repo, task) == 1
    assert not await repo.db.ssh_connection_slots.find_one({"_id": "192.0.2.9", "claims.taskId": "task-1"})


def test_ssh_admission_normalizes_ip_and_rejects_hostname():
    assert normalize_ssh_address("2001:0db8::9") == "2001:db8::9"
    with pytest.raises(ValueError, match="IPv4 或 IPv6"):
        normalize_ssh_address("camera.example.test")


@pytest.mark.asyncio
async def test_unknown_admission_result_never_retries_or_allows_socket_creation():
    """驱动网络异常后保留原 token，调用方只能隔离确认，不能生成第二个占位。"""
    class FailingSlots:
        def __init__(self):
            self.calls = 0

        async def find_one_and_update(self, *_args, **_kwargs):
            self.calls += 1
            raise AutoReconnect("connection reset")

    slots = FailingSlots()
    admission = SshAdmission(SimpleNamespace(db=SimpleNamespace(ssh_connection_slots=slots)), _task(1))
    with pytest.raises(SshSlotUncertain) as first:
        await admission.acquire()
    with pytest.raises(SshSlotUncertain) as second:
        await admission.acquire()
    assert first.value.token == second.value.token
    assert slots.calls == 1


@pytest.mark.asyncio
async def test_cancelled_admission_is_converted_to_unknown_slot_result():
    """数据库调用被取消时服务端是否已写入未知，必须禁止继续建连。"""
    class CancelledSlots:
        async def find_one_and_update(self, *_args, **_kwargs):
            raise asyncio.CancelledError()

    admission = SshAdmission(SimpleNamespace(db=SimpleNamespace(ssh_connection_slots=CancelledSlots())), _task(1))
    with pytest.raises(SshSlotUncertain):
        await admission.acquire()
