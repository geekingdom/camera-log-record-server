"""跨 Worker 的 SSH 设备连接名额准入。

海康设备按网络地址和端口最多接受五条 SSH 连接。单个 MongoDB 文档保存一个端点的
全部占位，原子条件更新保证不同 Worker 不会各自认为还有余量。历史默认22端口沿用
仅含 IP 的文档键，避免升级时丢失既有占位；其它端口使用独立端点键。占位没有 TTL：
进程失联时自动回收可能让仍存活的 socket 与新连接并存，必须等明确关闭或隔离收据后释放。
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

    def __init__(self, address: str, token: str, *, preconnect: bool = False,
                 admission_epoch: int | None = None) -> None:
        super().__init__(f"SSH 连接名额占位结果未知 address={address}")
        self.address = address
        self.token = token
        # 仅 acquire 在建立 socket 前设置。release 的未知结果绝不能据此推断连接不存在。
        self.preconnect = preconnect
        # 申请绑定的端点准入代次；协调递增后，迟到的旧代次写入不能重新占位。
        self.admission_epoch = admission_epoch


def normalize_ssh_address(value: Any) -> str:
    """统一 IPv4/IPv6 字面量，作为端点键及历史默认端口键的地址部分。"""
    try:
        return str(ip_address(str(value).strip()))
    except ValueError as error:
        raise ValueError("SSH 连接地址必须是有效 IPv4 或 IPv6 地址") from error


def normalize_ssh_endpoint(value: Any, port: Any = 22) -> str:
    """返回稳定端点键；22沿用历史IP键，IPv6其它端口使用方括号避免歧义。"""
    address = normalize_ssh_address(value)
    try:
        number = int(port)
    except (TypeError, ValueError) as error:
        raise ValueError("SSH 连接端口必须是1至65535的整数") from error
    if not 1 <= number <= 65535:
        raise ValueError("SSH 连接端口必须是1至65535的整数")
    if number == 22:
        # 旧版本的 claim 未记录端口，按默认SSH端口保守解释，不能升级后丢失占位。
        return address
    return f"[{address}]:{number}" if ":" in address else f"{address}:{number}"


def _owner(task: dict[str, Any], token: str, admission_epoch: int | None = None) -> dict[str, Any]:
    """固定占位身份，旧运行和旧节点永远不能释放新运行的连接名额。"""
    required = ("id", "runId", "generation", "nodeId")
    if any(task.get(key) is None for key in required):
        raise ValueError("SSH 连接名额需要任务、运行、代次和节点身份")
    claim = {
        "taskId": str(task["id"]), "runId": str(task["runId"]),
        "generation": task["generation"], "nodeId": str(task["nodeId"]),
        "token": token, "port": int(task.get("port", 22)), "claimedAt": now(),
    }
    if admission_epoch is not None:
        claim["admissionEpoch"] = admission_epoch
    return claim


def _epoch_filter(endpoint: str, admission_epoch: int) -> dict[str, Any]:
    """构造绑定端点准入代次的 CAS 条件，兼容未迁移的历史槽位文档。"""
    condition: dict[str, Any] = {"_id": endpoint}
    if admission_epoch == 0:
        condition["$or"] = [{"admissionEpoch": 0}, {"admissionEpoch": {"$exists": False}}]
    else:
        condition["admissionEpoch"] = admission_epoch
    return condition


class SshAdmission:
    """持有一次可精确释放的 SSH 名额；同一实例不会重复申请。"""

    def __init__(self, repo: Any, task: dict[str, Any]) -> None:
        self.repo = repo
        self.task = task
        self.address = normalize_ssh_address(task.get("ip"))
        self.endpoint = normalize_ssh_endpoint(task.get("ip"), task.get("port", 22))
        self._claim: dict[str, Any] | None = None
        self._uncertain = False

    async def acquire(self) -> str:
        """原子占用一个名额；满额或提交未知时均拒绝建立 SSH socket。"""
        if self._uncertain:
            # 使用未知 token 不能证明已占位成功，也不能生成新 token 后造成双占位。
            raise SshSlotUncertain(self.endpoint, self._claim["token"] if self._claim else "")
        if self._claim is not None:
            return self._claim["token"]
        token = new_id()
        claim = _owner(self.task, token)
        admission_epoch: int | None = None
        for attempt in range(_INITIAL_UPSERT_RETRIES):
            try:
                state = await self.repo.db.ssh_connection_slots.find_one(
                    {"_id": self.endpoint}, {"admissionEpoch": 1},
                )
                admission_epoch = int(state.get("admissionEpoch", 0)) if state else 0
                claim = _owner(self.task, token, admission_epoch)
                condition = _epoch_filter(self.endpoint, admission_epoch)
                condition[f"claims.{MAX_SSH_CONNECTIONS_PER_DEVICE - 1}"] = {"$exists": False}
                slot = await self.repo.db.ssh_connection_slots.find_one_and_update(
                    condition,
                    {"$push": {"claims": claim}, "$set": {"updatedAt": now(),
                                                              "admissionEpoch": admission_epoch}},
                    upsert=True, return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                # 同地址首个文档的并发 upsert 有唯一键竞争；重试后走已有文档条件更新。
                if attempt + 1 == _INITIAL_UPSERT_RETRIES:
                    raise SshCapacityError(f"设备端点 {self.endpoint} 的 SSH 连接名额已满") from None
                continue
            except asyncio.CancelledError as error:
                # 取消可能发生在服务端已接收更新之后；按结果未知处理，绝不能建 socket。
                self._claim, self._uncertain = claim, True
                raise SshSlotUncertain(self.endpoint, token, preconnect=True,
                                       admission_epoch=admission_epoch) from error
            except (PyMongoError, TimeoutError) as error:
                self._claim, self._uncertain = claim, True
                raise SshSlotUncertain(self.endpoint, token, preconnect=True,
                                       admission_epoch=admission_epoch) from error
            if slot is None:
                # 同一端点的协调刚递增代次时，当前已确认失败的申请可用同 token 重新
                # 绑定新代次；提交未知的申请会在异常分支进入隔离，绝不重读后复活。
                try:
                    current = await self.repo.db.ssh_connection_slots.find_one(
                        {"_id": self.endpoint}, {"admissionEpoch": 1},
                    )
                except asyncio.CancelledError as error:
                    self._claim, self._uncertain = claim, True
                    raise SshSlotUncertain(self.endpoint, token, preconnect=True) from error
                except (PyMongoError, TimeoutError) as error:
                    self._claim, self._uncertain = claim, True
                    raise SshSlotUncertain(self.endpoint, token, preconnect=True,
                                           admission_epoch=admission_epoch) from error
                if current is not None and int(current.get("admissionEpoch", 0)) != admission_epoch:
                    continue
                raise SshCapacityError(f"设备端点 {self.endpoint} 的 SSH 连接名额已满")
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
                {"_id": self.endpoint, "claims": {"$elemMatch": claim}},
                {"$pull": {"claims": claim}, "$set": {"updatedAt": now()}},
            )
        except asyncio.CancelledError:
            # 取消关闭无法证明 $pull 是否已提交，保留 token 供运行时进入 BLOCKED。
            self._uncertain = True
            raise
        except (PyMongoError, TimeoutError) as error:
            self._uncertain = True
            raise SshSlotUncertain(self.endpoint, claim["token"]) from error
        self._claim = None
        self._uncertain = False
        return bool(result.modified_count)


async def reconcile_preconnect_uncertain_slot(repo: Any, task: dict[str, Any], uncertainty: SshSlotUncertain) -> bool:
    """精确回收尚未建立 socket 的未知申请，结果仍未知时保持隔离。

    该路径先原子递增端点的准入代次，再按运行身份精确 $pull。Mongo 单文档操作串行化：
    先提交的旧代次 acquire 会被随后 $pull 移除，后提交的旧代次 acquire 因 CAS 不匹配
    无法写入。代次为单个整数，不保留无限增长的 token 墓碑。
    """
    if not uncertainty.preconnect:
        raise ValueError("非预建连 SSH 未知结果不能自动协调")
    endpoint = normalize_ssh_endpoint(task.get("ip"), task.get("port", 22))
    claim = _owner(task, uncertainty.token, uncertainty.admission_epoch)
    # claimedAt 是调用本地时间，协调时不能重建后要求相等；其余身份和 token 不可省略。
    exact = {key: value for key, value in claim.items() if key != "claimedAt"}
    try:
        await repo.db.ssh_connection_slots.update_one(
            {"_id": endpoint},
            {"$inc": {"admissionEpoch": 1}, "$set": {"updatedAt": now()}},
            upsert=True,
        )
        await repo.db.ssh_connection_slots.update_one(
            {"_id": endpoint},
            {"$pull": {"claims": exact}, "$set": {"updatedAt": now()}},
        )
    except asyncio.CancelledError as error:
        raise SshSlotUncertain(endpoint, uncertainty.token, preconnect=True) from error
    except DuplicateKeyError as error:
        # acquire 与围栏首次建文档竞争时，无法确认围栏是否已经落库，保持隔离。
        raise SshSlotUncertain(endpoint, uncertainty.token, preconnect=True) from error
    except (PyMongoError, TimeoutError) as error:
        raise SshSlotUncertain(endpoint, uncertainty.token, preconnect=True) from error
    return True


async def release_task_slots(repo: Any, task: dict[str, Any], session: Any = None) -> int:
    """在明确关闭或隔离收据后，按任务运行和代次回收全部遗留 SSH 占位。

    此函数不检查任务状态，也不根据心跳、租约或超时猜测 socket 已关闭；调用方必须先
    获得关闭收据或管理员隔离证明。节点 ID 不进入删除条件，以便节点失联后的已确认
    隔离路径能收回该旧运行占位。
    """
    required = ("id", "runId", "generation")
    if any(task.get(key) is None for key in required):
        raise ValueError("释放 SSH 名额需要任务、运行和代次身份")
    owner = {"taskId": str(task["id"]), "runId": str(task["runId"]), "generation": task["generation"]}
    # 升级前非22任务也落在IP键，且任务端口可在运行后被编辑。按不可变运行身份扫描
    # 全部端点键，才能在关闭收据或隔离确认后精确回收实际占位而不误删后继代次。
    result = await repo.db.ssh_connection_slots.update_many(
        {"claims": {"$elemMatch": owner}},
        {"$pull": {"claims": owner},
         "$set": {"updatedAt": now()}},
        session=session,
    )
    return int(result.modified_count)
