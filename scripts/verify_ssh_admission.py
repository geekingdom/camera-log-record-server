"""在真实 MongoDB 副本集验证跨进程 SSH 连接名额。

脚本只建立随机临时数据库中的名额元数据，不连接设备、不创建 socket，也不读取
平台任务。两个独立进程同时占用同一 IP 的不同端口，用来确认五个名额属于设备
网络地址而非某个 Worker 或端口。所有占位在 finally 中随临时数据库删除。
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from multiprocessing import Barrier, Queue, get_context
from queue import Empty
from typing import Any
from uuid import uuid4

from camera_logs.collection.ssh_admission import (
    SshAdmission,
    SshCapacityError,
    release_task_slots,
)
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from cryptography.fernet import Fernet
from pymongo import AsyncMongoClient


def task(identifier: str, *, address: str, port: int, run: str | None = None,
         generation: int = 1, node: str = "verify-node") -> dict[str, Any]:
    """构造仅含名额身份的虚拟任务，端口只用于证明它不参与配额键。"""
    return {"id": identifier, "ip": address, "port": port, "runId": run or f"{identifier}-run",
            "generation": generation, "nodeId": node, "protocol": "SSH"}


async def _claim_batch(uri: str, database_name: str, entries: list[dict[str, Any]]) -> list[str]:
    """在一个独立 Worker 进程中并发申请一组名额，故意不释放占位。"""
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    try:
        settings = Settings(_env_file=None, mongo_uri=uri, database_name=database_name,
                            encryption_key=Fernet.generate_key().decode(), start_background=False)
        repository = Repository(client[database_name], settings)
        admissions = [SshAdmission(repository, item) for item in entries]
        return list(await asyncio.gather(*(admission.acquire() for admission in admissions)))
    finally:
        await client.close()


def _process_claims(uri: str, database_name: str, entries: list[dict[str, Any]],
                    barrier: Barrier, output: Queue) -> None:
    """进程入口；屏障保证两个 Worker 的首次 upsert 发生竞争。"""
    barrier.wait(timeout=20)
    output.put({"tokens": asyncio.run(_claim_batch(uri, database_name, entries))})


async def verify(uri: str) -> dict[str, object]:
    """执行跨进程占位、明确释放和后继隔离断言，最后删除整个随机库。"""
    database_name = f"ssh_admission_verify_{uuid4().hex}"
    client = AsyncMongoClient(uri, serverSelectionTimeoutMS=5000, tz_aware=True, w="majority", journal=True)
    processes = []
    try:
        hello = await client.admin.command("hello")
        if not hello.get("setName"):
            raise RuntimeError("SSH 名额跨进程验证需要 MongoDB 副本集")

        address = "198.51.100.19"
        entries = [task(f"same-ip-{index}", address=address, port=port, node=f"node-{index % 2}")
                   for index, port in enumerate((22, 23, 2200, 2222, 2022), start=1)]
        context = get_context("spawn")
        barrier, output = context.Barrier(3), context.Queue()
        for group in (entries[:3], entries[3:]):
            process = context.Process(target=_process_claims, args=(uri, database_name, group, barrier, output))
            process.start()
            processes.append(process)
        # 主进程只参与同步，不触碰占位集合，确保竞争来自两个独立解释器。
        barrier.wait(timeout=20)
        for process in processes:
            process.join(timeout=30)
            if process.exitcode != 0:
                raise AssertionError(f"SSH 名额验证子进程异常退出 exitCode={process.exitcode}")
        outcomes = []
        for _ in processes:
            try:
                outcomes.append(output.get(timeout=5))
            except Empty as error:
                raise AssertionError("SSH 名额验证子进程没有返回结果") from error
        if len({token for item in outcomes for token in item["tokens"]}) != 5:
            raise AssertionError(f"同 IP 名额 token 不是五个唯一值 outcomes={outcomes!r}")

        settings = Settings(_env_file=None, mongo_uri=uri, database_name=database_name,
                            encryption_key=Fernet.generate_key().decode(), start_background=False)
        repo = Repository(client[database_name], settings)
        slot = await repo.db.ssh_connection_slots.find_one({"_id": address})
        if not slot or len(slot.get("claims", [])) != 5:
            raise AssertionError(f"同 IP 不同端口未共享五个名额 slot={slot!r}")
        try:
            await SshAdmission(repo, task("sixth", address=address, port=10022)).acquire()
        except SshCapacityError:
            pass
        else:
            raise AssertionError("同 IP 已有五路占位时第六路仍被允许")

        # 将所有占位伪造为陈旧值后再次申请；无 TTL/时间回收策略必须仍拒绝第六路。
        old = now() - timedelta(days=365)
        await repo.db.ssh_connection_slots.update_one(
            {"_id": address}, {"$set": {"updatedAt": old, "claims.$[].claimedAt": old}},
        )
        try:
            await SshAdmission(repo, task("stale-sixth", address=address, port=10023)).acquire()
        except SshCapacityError:
            pass
        else:
            raise AssertionError("未知或陈旧 SSH 占位被时间自动回收")

        # 正常 release 必须立即还回一个 token，新的占位才可进入已满设备。
        releasable = SshAdmission(repo, task("release-owner", address="198.51.100.20", port=22))
        await releasable.acquire()
        if not await releasable.release():
            raise AssertionError("明确 token 释放没有修改名额")
        if not await SshAdmission(repo, task("release-successor", address="198.51.100.20", port=2022)).acquire():
            raise AssertionError("释放 token 后不能重新申请名额")

        # 相同任务 ID 的不同 run/generation 可共存；释放旧身份不得带走后继 claim。
        predecessor = task("reuse-task", address="198.51.100.21", port=22, run="run-old", generation=7)
        successor = task("reuse-task", address="198.51.100.21", port=22, run="run-new", generation=8)
        old_admission, new_admission = SshAdmission(repo, predecessor), SshAdmission(repo, successor)
        await old_admission.acquire()
        successor_token = await new_admission.acquire()
        if not await release_task_slots(repo, predecessor):
            raise AssertionError("精确旧运行释放没有删除旧占位")
        isolated = await repo.db.ssh_connection_slots.find_one({"_id": "198.51.100.21"})
        if not isolated or [claim["token"] for claim in isolated.get("claims", [])] != [successor_token]:
            raise AssertionError(f"旧运行释放影响了后继名额 slot={isolated!r}")
        return {"passed": True, "temporaryDatabase": database_name, "crossProcess": True,
                "sameIpPorts": [item["port"] for item in entries], "unknownClaimsRetained": True,
                "exactReleaseIsolation": True, "noDeviceAccess": True}
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)
        await client.drop_database(database_name)
        deleted = database_name not in await client.list_database_names()
        await client.close()
        if not deleted:
            raise AssertionError("SSH 名额验证临时数据库没有删除")


if __name__ == "__main__":
    print(json.dumps(asyncio.run(verify(Settings().mongo_uri)), ensure_ascii=False, sort_keys=True))
