"""采集节点监督器：维护本机运行实例、异步释放任务和日志作业。

长时间归档独立于心跳周期执行，避免心跳超时取消最后的落盘。释放失败保留
归属并进入阻塞状态，不把超时当作旧连接已断开的证据。
"""
import asyncio
import logging
import shutil
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from pymongo import AsyncMongoClient, ReturnDocument

from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from camera_logs.common.ownership import owner_filter
from camera_logs.node.manual_queue import next_manual_command
from camera_logs.node.write_pressure import WRITE_LATENCY_LIMIT_MS, WritePressure

logger = logging.getLogger(__name__)


class Worker:
    """单个节点的运行容器；active 与 releases 都以稳定任务 ID 为键。"""
    def __init__(self, repo):
        self.repo = repo
        self.active = {}
        self.manual_jobs = {}
        self.jobs = set()
        self.last_database_ok = time.monotonic()
        self.last_bytes = 0
        self.last_tick = time.monotonic()
        self.last_maintenance = 0.0
        self.maintenance_task = None
        self.releases = {}
        self.disk_level = "NORMAL"
        self.write_pressure = WritePressure()

    def discard_closed(self, runtime):
        """仅移除已关闭的同一对象，等待期间安装的后继实例不得被旧回调移除。"""
        task_id = runtime.task["id"]
        if self.active.get(task_id) is runtime:
            self.active.pop(task_id)

    async def finish_runtime(self, runtime, action):
        """绑定旧实例处理异步收尾异常；隔离关闭不代表可以释放数据库归属。"""
        ownership = owner_filter(runtime.task)
        try:
            if action == "isolate":
                await runtime.stop()
                self.discard_closed(runtime)
            elif action == "pause":
                await self.pause(runtime)
            else:
                await self.release(runtime)
        except Exception:
            if action != "isolate":
                changed = await self.repo.db.tasks.update_one(ownership, {"$set": {
                    "status": "BLOCKED", "error": "连接或日志关闭未完成，禁止重新连接",
                }})
                if changed.matched_count:
                    await self.repo.db.operations.update_many(
                        {"taskId": ownership["id"], "status": "PENDING"},
                        {"$set": {"status": "FAILED", "completedAt": now()}},
                    )
            raise

    def write_latency(self):
        """汇总活动会话的最近写入快照；最慢会话决定新会话准入。"""
        snapshots = []
        for runtime in self.active.values():
            collector = getattr(runtime, "collector", None)
            tracker = getattr(collector, "write_latency", None)
            if tracker is not None:
                snapshots.append(tracker.snapshot())
        p99_ms = max((float(snapshot["p99Ms"]) for snapshot in snapshots), default=0.0)
        pending_ms = max((float(snapshot["pendingMs"]) for snapshot in snapshots), default=0.0)
        return {
            "writeLatencyMs": max(p99_ms, pending_ms),
            "writeLatencySamples": sum(int(snapshot["samples"]) for snapshot in snapshots),
            "writeLatencyPendingMs": pending_ms,
            "writeLatencyWindowSeconds": 60,
        }

    async def report_disk_pressure(self, percent):
        """仅在磁盘阈值级别变化时记录告警及恢复，避免每秒重复事件。"""
        level = "CRITICAL" if percent >= 95 else "NO_ADMISSION" if percent >= 90 else "WARNING" if percent >= 80 else "NORMAL"
        if level == self.disk_level:
            return
        await self.repo.db.events.insert_one({"type": "DISK_PRESSURE_CHANGED", "nodeId": self.repo.settings.node_id,
            "level": level, "previousLevel": self.disk_level, "diskPercent": percent, "createdAt": now()})
        log = logger.info if level == "NORMAL" else logger.warning
        log("节点磁盘状态变化 node=%s level=%s used=%.2f%%", self.repo.settings.node_id, level, percent)
        self.disk_level = level

    async def stop_pending(self, task):
        """取消尚未建连的本机任务；无会话需要关闭，可直接结束运行并释放端点锁。"""
        changed = await self.repo.db.tasks.update_one(
            {**owner_filter(task), "nodeId": self.repo.settings.node_id,
             "status": "PENDING", "desiredState": "STOPPED"},
            {"$set": {"status": "STOPPED", "nodeId": None, "updatedAt": now()}})
        if not changed.matched_count:
            return
        await self.repo.db.endpoint_locks.delete_one({"taskId": task["id"], "runId": task["runId"]})
        await self.repo.db.runs.update_one({"id": task["runId"]}, {"$set": {"endedAt": now()}})
        await self.repo.db.operations.update_many(
            {"taskId": task["id"], "desiredState": "STOPPED", "status": "PENDING"},
            {"$set": {"status": "SUCCEEDED", "completedAt": now()}})
        await self.repo.db.operations.update_many(
            {"taskId": task["id"], "desiredState": "RUNNING", "status": "PENDING"},
            {"$set": {"status": "FAILED", "error": "启动前已停止", "completedAt": now()}})
        # 编辑排队任务也沿用受控重启语义；显式停止已由 API 清除 restartRequested。
        await self.repo.db.tasks.update_one(
            {**owner_filter(task), "nodeId": None, "status": "STOPPED", "restartRequested": True},
            {"$set": {"desiredState": "RUNNING", "restartRequested": False}})

    async def pause(self, runtime):
        """等待连接关闭和日志排空，保留运行预算与端点锁；并发停止优先完成释放。"""
        task = runtime.task
        await runtime.stop()
        changed = await self.repo.db.tasks.update_one(
            {**owner_filter(task), "desiredState": "PAUSED"},
            {"$set": {"status": "PAUSED", "nodeId": None, "pausedAt": now()}})
        if not changed.matched_count:
            await self.release(runtime)
            return
        await self.repo.db.operations.update_many({"taskId": task["id"], "desiredState": "PAUSED", "status": "PENDING"},
            {"$set": {"status": "SUCCEEDED", "completedAt": now()}})
        await self.repo.db.events.insert_one({"taskId": task["id"], "runId": task["runId"], "type": "USER_PAUSED", "createdAt": now()})
        self.discard_closed(runtime)

    async def pause_pending(self, task):
        """任务已领取但尚未建连时直接暂停；与普通暂停一样保留运行及端点锁。"""
        changed = await self.repo.db.tasks.update_one(
            {**owner_filter(task), "desiredState": "PAUSED", "status": "PENDING"},
            {"$set": {"status": "PAUSED", "nodeId": None, "pausedAt": now()}},
        )
        if not changed.matched_count:
            return
        await self.repo.db.operations.update_many(
            {"taskId": task["id"], "desiredState": "PAUSED", "status": "PENDING"},
            {"$set": {"status": "SUCCEEDED", "completedAt": now()}},
        )
        await self.repo.db.events.insert_one(
            {"taskId": task["id"], "runId": task["runId"], "type": "USER_PAUSED", "createdAt": now()}
        )

    async def release(self, runtime):
        """结束运行后移除端点锁，持久化操作结果；关闭异常必须向监督周期传播。"""
        task = runtime.task
        await runtime.stop()
        failed = runtime.error or runtime.background_failure()
        update = {"nodeId": None, "status": "ERROR" if failed else "STOPPED", "error": str(failed) if failed else None, "updatedAt": now()}
        if failed:
            update["desiredState"] = "STOPPED"
        # 数据库收尾期间保留归属，防止中途失败后无法凭原领取身份继续完成清理。
        closing = {key: value for key, value in update.items() if key != "nodeId"}
        closing["status"] = "STOPPING"
        changed = await self.repo.db.tasks.update_one(owner_filter(task), {"$set": closing})
        if not changed.matched_count:
            # 物理连接已确认关闭，但数据库归属已改变；不得释放后继的锁或结束其操作。
            self.discard_closed(runtime)
            logger.info("旧运行实例已关闭，跳过过期归属回写 task=%s run=%s generation=%s",
                        task["id"], task["runId"], task.get("generation"))
            return
        await self.repo.db.endpoint_locks.delete_one({"taskId": task["id"], "runId": task["runId"]})
        await self.repo.db.runs.update_one({"id": task["runId"]}, {"$set": {"endedAt": now()}})
        await self.repo.db.operations.update_many({"taskId": task["id"], "desiredState": "STOPPED", "status": "PENDING"},
            {"$set": {"status": "FAILED" if failed else "SUCCEEDED", "completedAt": now()}})
        if failed:
            await self.repo.db.operations.update_many({"taskId": task["id"], "desiredState": "RUNNING", "status": "PENDING"},
                {"$set": {"status": "FAILED", "completedAt": now()}})
        await self.repo.db.tasks.update_one(owner_filter(task), {"$set": update})
        await self.repo.db.tasks.find_one_and_update(
            {**owner_filter(task), "nodeId": None, "status": update["status"], "restartRequested": True},
            {"$set": {"desiredState": "RUNNING", "restartRequested": False}}, return_document=ReturnDocument.AFTER)
        self.discard_closed(runtime)

    async def isolate_active_sessions(self):
        """数据库长期失联时逐一关闭本机连接，不凭异常假定端点已经释放。

        成功停止的实例才从 active 移除；停止失败的实例保留，后续人工或恢复后的
        周期可继续隔离，端点锁也不会被本节点错误删除。
        """
        for task_id, runtime in list(self.active.items()):
            try:
                await runtime.stop()
            except Exception:
                logger.exception("数据库失联时关闭采集实例失败 task=%s", task_id)
            else:
                if self.active.get(task_id) is runtime:
                    self.active.pop(task_id)

    async def tick(self):
        """更新资源心跳、处理期望状态并分发作业，不在本周期等待耗时归档。"""
        from camera_logs.collection.connections import connect
        root = self.repo.settings.log_root
        root.mkdir(parents=True, exist_ok=True)
        disk = shutil.disk_usage(root)
        disk_percent = disk.used / disk.total * 100
        await self.report_disk_pressure(disk_percent)
        # 人工配置独立于心跳保存；变更只控制新建连接，不强制中断已有采集。
        config = await self.repo.db.node_configs.find_one({"id": self.repo.settings.node_id}) or {}
        capacity = min(100, self.repo.settings.node_capacity, config.get("capacity", self.repo.settings.node_capacity))
        mismatch = bool(config.get("url") and config["url"].rstrip("/") != self.repo.settings.node_url.rstrip("/"))
        reported = await self.repo.db.nodes.find_one({"id": self.repo.settings.node_id}) or {}
        write_latency = self.write_latency()
        await self.write_pressure.report(self.repo.db, self.repo.settings.node_id, write_latency)
        accepting = (
            disk_percent < 90
            and config.get("accepting", True)
            and not mismatch
            and not reported.get("isolated", False)
            and write_latency["writeLatencyMs"] <= WRITE_LATENCY_LIMIT_MS
        )
        current_bytes = sum(r.input_bytes for r in self.active.values())
        tick = time.monotonic()
        rate = max(0, current_bytes-self.last_bytes)/max(.01, tick-self.last_tick)
        self.last_bytes, self.last_tick = current_bytes, tick
        await self.repo.db.nodes.update_one({"id": self.repo.settings.node_id}, {"$set": {
            "id": self.repo.settings.node_id, "url": self.repo.settings.node_url, "heartbeat": now(),
            "capacity": capacity, "activeTasks": len(self.active),
            "diskPercent": disk_percent, "diskFreeBytes": disk.free, "inputBytesPerSecond": rate,
            **write_latency,
            "accepting": accepting, "configurationMismatch": mismatch,
            "configuredUrl": config.get("url")}}, upsert=True)
        for task_id, future in list(self.releases.items()):
            if future.done():
                try:
                    future.result()
                except Exception:
                    logger.exception("运行实例释放失败 task=%s", task_id)
                self.releases.pop(task_id, None)
        tasks = [t async for t in self.repo.db.tasks.find({"nodeId": self.repo.settings.node_id})]
        assigned = {task["id"]: task for task in tasks}
        # 仅成功取得数据库快照后核对归属；读取失败不能被解释成任务已经消失。
        for task_id, runtime in list(self.active.items()):
            current = assigned.get(task_id)
            if task_id not in self.releases and (
                current is None or owner_filter(runtime.task) != owner_filter(current)
                or current["status"] == "BLOCKED" or reported.get("isolated", False)
            ):
                runtime.retired = True
                runtime.stopping = True
                self.releases[task_id] = asyncio.create_task(self.finish_runtime(runtime, "isolate"))
        for task in tasks:
            if task["id"] in self.releases:
                continue
            runtime = self.active.get(task["id"])
            if task["status"] == "BLOCKED":
                continue
            if runtime and task["desiredState"] == "PAUSED":
                await self.repo.db.tasks.update_one(owner_filter(task), {"$set": {"status": "PAUSING"}})
                self.releases[task["id"]] = asyncio.create_task(self.finish_runtime(runtime, "pause"))
                continue
            if runtime is None and task["desiredState"] == "PAUSED" and task["status"] == "PENDING":
                await self.pause_pending(task)
                continue
            if runtime is None and task["desiredState"] == "STOPPED" and task["status"] == "PENDING":
                await self.stop_pending(task)
                continue
            if runtime and (task["desiredState"] == "STOPPED" or disk_percent >= 95 or runtime.background.done()):
                if disk_percent >= 95:
                    runtime.error = "磁盘空间不足，已停止采集"
                self.releases[task["id"]] = asyncio.create_task(self.finish_runtime(runtime, "release"))
                continue
            elif runtime is None and task["status"] == "PENDING" and task["desiredState"] == "RUNNING":
                # 调度心跳可能已经过期，建连前以本周期磁盘值复核；保留排队任务直到空间恢复。
                if not accepting or len(self.active) >= capacity:
                    continue
                self.active[task["id"]] = SessionRuntime(self.repo, task, connect)
            elif runtime is None and task["status"] not in ("STOPPED", "BLOCKED"):
                await self.repo.db.tasks.update_one(owner_filter(task),
                    {"$set": {"status": "BLOCKED", "error": "运行实例已丢失，等待隔离确认"}})
            if runtime and not runtime.stopping and task["status"] == "COLLECTING":
                manual = self.manual_jobs.get(task["id"])
                if manual is None or manual.done():
                    command = await next_manual_command(self.repo, runtime)
                    if command:
                        self.manual_jobs[task["id"]] = asyncio.create_task(runtime.manual(command))
        self.jobs = {job for job in self.jobs if not job.done()}
        if len(self.jobs) < 2:
            job = await self.repo.db.jobs.find_one_and_update({"nodeId": self.repo.settings.node_id, "status": "QUEUED"},
                {"$set": {"status": "RUNNING", "startedAt": now()}}, return_document=ReturnDocument.AFTER)
            if job:
                from camera_logs.logs.jobs import run_job
                self.jobs.add(asyncio.create_task(run_job(self.repo, job)))
        if time.monotonic() - self.last_maintenance >= 60:
            from camera_logs.logs.maintenance import maintain

            self.last_maintenance = time.monotonic()
            if self.maintenance_task is None or self.maintenance_task.done():
                self.maintenance_task = asyncio.create_task(maintain(self.repo))
        self.last_database_ok = time.monotonic()

    async def run(self):
        """持续执行节点周期；长期失去数据库联系时主动关闭本机连接。"""
        while True:
            try:
                await asyncio.wait_for(self.tick(), timeout=10)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("节点周期失败")
                if time.monotonic()-self.last_database_ok > 15:
                    await self.isolate_active_sessions()
            await asyncio.sleep(1)

    async def close(self):
        """进程退出时等待所有连接和文件关闭，再回收查询与维护协程。"""
        await asyncio.gather(*self.releases.values(), return_exceptions=True)
        for runtime in list(self.active.values()):
            try:
                if getattr(runtime, "retired", False):
                    await self.finish_runtime(runtime, "isolate")
                else:
                    await self.repo.db.tasks.update_one(owner_filter(runtime.task), {"$set": {"desiredState": "STOPPED"}})
                    await self.finish_runtime(runtime, "release")
            except Exception:
                logger.exception("节点停止失败")
        for job in self.jobs | set(self.manual_jobs.values()):
            job.cancel()
        tasks = [*self.jobs, *self.manual_jobs.values()]
        if self.maintenance_task:
            self.maintenance_task.cancel()
            tasks.append(self.maintenance_task)
        await asyncio.gather(*tasks, return_exceptions=True)


def create_worker_app(settings=None):
    """创建节点内部服务；数据库和运行日志资源与 ASGI 生命周期绑定。"""
    settings = settings or Settings()
    @asynccontextmanager
    async def lifespan(app):
        from camera_logs.common.observability import setup_logging
        listener = setup_logging(settings.log_root.parent / "service-logs" / settings.node_id)
        client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                                  w="majority", journal=True)
        repo = Repository(client[settings.database_name], settings)
        await repo.initialize()
        worker = Worker(repo)
        from camera_logs.logs.maintenance import recover_orphan_archives
        await recover_orphan_archives(repo)
        app.state.repo, app.state.worker = repo, worker
        from camera_logs.node.files import install_node_routes
        install_node_routes(app, repo, worker)
        task = asyncio.create_task(worker.run())
        yield
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await worker.close()
        from camera_logs.logs.compression import shutdown_compression
        await shutdown_compression()
        await client.close()
        listener.stop()

    app = FastAPI(title="采集节点内部服务", lifespan=lifespan)
    @app.get("/health")
    async def health():
        return {"status": "ok"}
    @app.get("/internal/tail/{task_id}")
    async def tail(task_id: str, request: Request, cursor: str = ""):
        import secrets
        if not settings.internal_token or not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer "+settings.internal_token):
            raise HTTPException(401)
        runtime = app.state.worker.active.get(task_id)
        return runtime.tail(cursor) if runtime else {"frames": [], "gap": False}
    return app


if __name__ == "__main__":
    uvicorn.run(create_worker_app(), host="0.0.0.0", port=8001)
