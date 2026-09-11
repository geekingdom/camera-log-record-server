"""跨 Worker 的 SSH 设备连接名额准入。

海康设备按网络地址最多接受五条 SSH 连接。单个 MongoDB 文档保存一个地址的全部
占位，原子条件更新保证不同 Worker 不会各自认为还有余量。占位没有 TTL：进程失联
时自动回收可能让仍存活的 socket 与新连接并存，必须等明确关闭或隔离收据后释放。
"""

from __future__ import annotations

import asyncio
from ipaddress import ip_address
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from camera_logs.common.database import now
from camera_logs.common.models import new_id

MAX_SSH_CONNECTIONS_PER_DEVICE = 5
_INITIAL_UPSERT_RETRIES = 3


class SshCapacityError(RuntimeError):
    """目标设备的五个 SSH 名额均已被明确占用。"""


class SshSlotUncertain(RuntimeError):
    """Mongo 提交结果未知，调用方不得建立 socket 或主动重试占位。"""

    def __init__(self, address: str, token: str) -> None:
        super().__init__(f"SSH 连接名额占位结果未知 address={address}")
        self.address = address
        self.token = token


def normalize_ssh_address(value: Any) -> str:
    """统一 IPv4/IPv6 字面量，确保同 IP 的不同端口共享五个名额。"""
    try:
        return str(ip_address(str(value).strip()))
    except ValueError as error:
        raise ValueError("SSH 连接地址必须是有效 IPv4 或 IPv6 地址") from error


def _owner(task: dict[str, Any], token: str) -> dict[str, Any]:
    """固定占位身份，旧运行和旧节点永远不能释放新运行的连接名额。"""
    required = ("id", "runId", "generation", "nodeId")
    if any(task.get(key) is None for key in required):
        raise ValueError("SSH 连接名额需要任务、运行、代次和节点身份")
    return {
        "taskId": str(task["id"]), "runId": str(task["runId"]),
        "generation": task["generation"], "nodeId": str(task["nodeId"]),
        "token": token, "claimedAt": now(),
    }


class SshAdmission:
    """持有一次可精确释放的 SSH 名额；同一实例不会重复申请。"""

    def __init__(self, repo: Any, task: dict[str, Any]) -> None:
        self.repo = repo
        self.task = task
        self.address = normalize_ssh_address(task.get("ip"))
        self._claim: dict[str, Any] | None = None
        self._uncertain = False

    async def acquire(self) -> str:
        """原子占用一个名额；满额或提交未知时均拒绝建立 SSH socket。"""
        if self._uncertain:
            # 使用未知 token 不能证明已占位成功，也不能生成新 token 后造成双占位。
            raise SshSlotUncertain(self.address, self._claim["token"] if self._claim else "")
        if self._claim is not None:
            return self._claim["token"]
        token = new_id()
        claim = _owner(self.task, token)
        for attempt in range(_INITIAL_UPSERT_RETRIES):
            try:
                slot = await self.repo.db.ssh_connection_slots.find_one_and_update(
                    {"_id": self.address, f"claims.{MAX_SSH_CONNECTIONS_PER_DEVICE - 1}": {"$exists": False}},
                    {"$push": {"claims": claim}, "$set": {"updatedAt": now()}},
                    upsert=True, return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                # 同地址首个文档的并发 upsert 有唯一键竞争；重试后走已有文档条件更新。
                if attempt + 1 == _INITIAL_UPSERT_RETRIES:
                    raise SshCapacityError(f"设备 {self.address} 的 SSH 连接名额已满") from None
                continue
            except asyncio.CancelledError as error:
                # 取消可能发生在服务端已接收更新之后；按结果未知处理，绝不能建 socket。
                self._claim, self._uncertain = claim, True
                raise SshSlotUncertain(self.address, token) from error
            except (PyMongoError, TimeoutError) as error:
                self._claim, self._uncertain = claim, True
                raise SshSlotUncertain(self.address, token) from error
            if slot is None:
                raise SshCapacityError(f"设备 {self.address} 的 SSH 连接名额已满")
            self._claim = claim
            return token
        raise AssertionError("SSH 名额申请重试循环未终止")

    async def release(self) -> bool:
        """仅释放本实例 token 对应的占位；重复释放安全且不会影响其它 Worker。"""
        if self._claim is None:
            return False
        claim = self._claim
        try:
            result = await self.repo.db.ssh_connection_slots.update_one(
                {"_id": self.address, "claims": {"$elemMatch": claim}},
                {"$pull": {"claims": claim}, "$set": {"updatedAt": now()}},
            )
        except asyncio.CancelledError:
            # 取消关闭无法证明 $pull 是否已提交，保留 token 供运行时进入 BLOCKED。
            self._uncertain = True
            raise
        except (PyMongoError, TimeoutError) as error:
            self._uncertain = True
            raise SshSlotUncertain(self.address, claim["token"]) from error
        self._claim = None
        self._uncertain = False
        return bool(result.modified_count)


async def release_task_slots(repo: Any, task: dict[str, Any], session: Any = None) -> int:
    """在明确关闭或隔离收据后，按任务运行和代次回收全部遗留 SSH 占位。

    此函数不检查任务状态，也不根据心跳、租约或超时猜测 socket 已关闭；调用方必须先
    获得关闭收据或管理员隔离证明。节点 ID 不进入删除条件，以便节点失联后的已确认
    隔离路径能收回该旧运行占位。
    """
    required = ("id", "runId", "generation")
    if any(task.get(key) is None for key in required):
        raise ValueError("释放 SSH 名额需要任务、运行和代次身份")
    address = normalize_ssh_address(task.get("ip"))
    owner = {"taskId": str(task["id"]), "runId": str(task["runId"]), "generation": task["generation"]}
    result = await repo.db.ssh_connection_slots.update_one(
        {"_id": address, "claims": {"$elemMatch": owner}},
        {"$pull": {"claims": owner},
         "$set": {"updatedAt": now()}},
        session=session,
    )
    return int(result.modified_count)
