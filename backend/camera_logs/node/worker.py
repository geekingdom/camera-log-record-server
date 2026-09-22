"""采集节点监督器：维护本机运行实例、异步释放任务和日志作业。

长时间归档独立于心跳周期执行，避免心跳超时取消最后的落盘。释放失败保留
归属并进入阻塞状态，不把超时当作旧连接已断开的证据。
"""
import asyncio
import logging
import shutil
import time
import uuid
from datetime import timedelta

from pymongo import ReturnDocument

from camera_logs.collection.runtime import SessionRuntime
from camera_logs.common.database import now
from camera_logs.common.ownership import owner_filter
from camera_logs.node.health import resource_pressure
from camera_logs.node.input_admission import input_rate_blocked, input_rate_limit
from camera_logs.node.manual_queue import next_manual_command
from camera_logs.node.recovery import finalize_closed_task, record_closed_receipt
from camera_logs.node.shutdown import mark_unavailable_for_shutdown, shutdown_runtimes
from camera_logs.node.telemetry_runtime import TelemetryRuntime
from camera_logs.node.write_pressure import WritePressure, write_latency_blocked, write_latency_limit

logger = logging.getLogger(__name__)


class Worker:
    """单个节点的运行容器；active 与 releases 都以稳定任务 ID 为键。"""
    def __init__(self, repo):
        self.repo = repo
        self.active = {}
        self.manual_jobs = {}
        self.manual_query_slots = asyncio.Semaphore(8)
        self.jobs = set()
        self.last_database_ok = time.monotonic()
        from camera_logs.node.input_admission import InputRateMeter
        self.input_rate = InputRateMeter()
        self.last_maintenance = 0.0
        self.maintenance_task = None
        self.releases = {}
        # 仅记录本实例亲自关闭过、但持久化收尾失败的运行；不能把任意 BLOCKED 当成可自动释放。
        self.failed_cleanups = {}
        self.self_fenced = {}
        # 数据库失联时自围栏的关闭失败必须由同一流程续做，不能落入普通 release。
        self.self_fence_pending = {}
        self.disk_level = "NORMAL"
        self.write_pressure = WritePressure()
        self.coredump_scanner = None
        self.coredump_scan_task = None
        self.last_coredump_scan = 0.0
        self.instance_id = uuid.uuid4().hex
        self.telemetry = TelemetryRuntime(repo.settings.host_proc_root)

    def discard_closed(self, runtime):
        """仅移除已关闭的同一对象，等待期间安装的后继实例不得被旧回调移除。"""
        task_id = runtime.task["id"]
        if self.active.get(task_id) is runtime:
            self.active.pop(task_id)
        if self.failed_cleanups.get(task_id, (None,))[0] is runtime:
            self.failed_cleanups.pop(task_id, None)

    def track_background(self, task, label):
        """记录后台扫描/导出异常，已完成任务必须取回异常而不能静默丢失。"""
        self.jobs.add(task)
        def completed(future):
            self.jobs.discard(future)
            try:
                future.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("节点后台任务失败 kind=%s", label)
        task.add_done_callback(completed)
        return task

    async def finish_runtime(self, runtime, action, *, already_stopped=False, stop_error=None):
        """绑定旧实例处理异步收尾异常；隔离关闭不代表可以释放数据库归属。"""
        ownership = owner_filter(runtime.task)
        try:
            if stop_error is not None:
                raise stop_error
            if action == "recover":
                await self.recover_blocked_runtime(runtime)
                return
            if action == "isolate":
                if not already_stopped:
                    await runtime.stop()
                if getattr(runtime, "collector", None) is not None:
                    await record_closed_receipt(
                        self.repo, runtime.task, self.instance_id, runtime.collector.session_id,
                    )
                self.discard_closed(runtime)
            elif action == "pause":
                await self.pause(runtime, already_stopped=already_stopped)
            else:
                await self.release(runtime, already_stopped=already_stopped)
        except Exception:
            if action != "isolate":
                changed = await self.repo.db.tasks.update_one(ownership, {"$set": {
                    "status": "BLOCKED", "error": "连接或日志关闭未完成，禁止重新连接",
                }})
                if changed.matched_count:
                    # 当前控制请求的关闭结果已无法确认；即使本进程后续补齐资源收尾，
                    # 也不能把这次失败的用户操作改写为成功，更不能跨进程留下 PENDING。
                    await self.repo.db.operations.update_many(
                        {"taskId": ownership["id"], "status": "PENDING"},
                        {"$set": {"status": "FAILED", "completedAt": now()}},
                    )
                    # 物理关闭可能已成功，只是日志或数据库后续步骤失败。下一轮只允许
                    # 同一运行以原动作续做收尾，归属变化后会由重试前的 CAS 拒绝。
                    self.failed_cleanups[runtime.task["id"]] = (runtime, action)
            raise

    async def recover_blocked_runtime(self, runtime):
        """显式恢复先确认旧连接关闭并固化收据，历史运行错误不能否决新的恢复意图。"""
        await runtime.stop()
        collector = getattr(runtime, "collector", None)
        if collector is None:
            raise RuntimeError("阻塞恢复缺少已关闭会话，不能确认旧运行")
        current = await self.repo.db.tasks.find_one(owner_filter(runtime.task), {"sessionId": 1})
        if current is None or current.get("sessionId") != collector.session_id:
            self.discard_closed(runtime)
            return
        await record_closed_receipt(self.repo, runtime.task, self.instance_id, collector.session_id)
        await finalize_closed_task(self.repo, runtime.task)
        self.discard_closed(runtime)

    async def retry_blocked_cleanup(self, runtime):
        """重试本 Worker 已知失败的收尾；未知隔离状态绝不在此路径释放。"""
        task_id = runtime.task["id"]
        pending = self.failed_cleanups.get(task_id)
        if pending is None or pending[0] is not runtime:
            return False
        current = await self.repo.db.tasks.find_one(owner_filter(runtime.task))
        if current is None or current.get("status") != "BLOCKED":
            if self.failed_cleanups.get(task_id) == pending:
                self.failed_cleanups.pop(task_id, None)
            return False
        await self.finish_runtime(runtime, pending[1])
        return True

    def write_latency(self):
        """汇总总等待和实际写盘快照；前者准入，后者供 AUTO 判断后台 I/O 压力。"""
        snapshots, io_snapshots = [], []
        for runtime in self.active.values():
            collector = getattr(runtime, "collector", None)
            tracker = getattr(collector, "write_latency", None)
            if tracker is not None:
                snapshots.append(tracker.snapshot())
            io_tracker = getattr(collector, "write_io_latency", None)
            if io_tracker is not None:
                io_snapshots.append(io_tracker.snapshot())
        p99_ms = max((float(snapshot["p99Ms"]) for snapshot in snapshots), default=0.0)
        pending_ms = max((float(snapshot["pendingMs"]) for snapshot in snapshots), default=0.0)
        io_p99_ms = max((float(snapshot["p99Ms"]) for snapshot in io_snapshots), default=0.0)
        io_pending_ms = max((float(snapshot["pendingMs"]) for snapshot in io_snapshots), default=0.0)
        return {
            "writeLatencyMs": max(p99_ms, pending_ms),
            "writeLatencySamples": sum(int(snapshot["samples"]) for snapshot in snapshots),
            "writeLatencyPendingMs": pending_ms,
            "writeLatencyWindowSeconds": 60,
            "writeIoLatencyMs": max(io_p99_ms, io_pending_ms),
            "writeIoLatencyP99Ms": io_p99_ms,
            "writeIoLatencySamples": sum(int(snapshot["samples"]) for snapshot in io_snapshots),
            "writeIoLatencyPendingMs": io_pending_ms,
            "writeIoLatencyWindowSeconds": 60,
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

    async def pause(self, runtime, *, already_stopped=False):
        """等待连接关闭和日志排空，保留运行预算与端点锁；并发停止优先完成释放。"""
        task = runtime.task
        if not already_stopped:
            await runtime.stop()
        changed = await self.repo.db.tasks.update_one(
            {**owner_filter(task), "desiredState": "PAUSED"},
            {"$set": {"status": "PAUSED", "nodeId": None, "pausedAt": now()}})
        if not changed.matched_count:
            await self.release(runtime)
            return
        await self.repo.db.operations.update_many({"taskId": task["id"], "desiredState": "PAUSED", "status": "PENDING"},
            {"$set": {"status": "SUCCEEDED", "completedAt": now()}})
        await self.repo.db.events.insert_one({"taskId": task["id"], "runId": task["runId"], "nodeId": self.repo.settings.node_id,
            "type": "USER_PAUSED", "createdAt": now()})
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
            {"taskId": task["id"], "runId": task["runId"], "nodeId": self.repo.settings.node_id,
             "type": "USER_PAUSED", "createdAt": now()}
        )

    async def release(self, runtime, *, already_stopped=False):
        """结束运行后移除端点锁，持久化操作结果；关闭异常必须向监督周期传播。"""
        task = runtime.task
        if not already_stopped:
            await runtime.stop()
        if getattr(runtime, "collector", None) is not None:
            await record_closed_receipt(self.repo, task, self.instance_id, runtime.collector.session_id)
        failed = runtime.error or runtime.background_failure()
        update = {"nodeId": None, "status": "ERROR" if failed else "STOPPED", "error": str(failed) if failed else None, "updatedAt": now()}
        if failed:
            # 已知运行故障不能兑现编辑重启；清除标记并保持停止，等待明确的新启动操作。
            update.update(desiredState="STOPPED", restartRequested=False)
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
        await self.repo.db.tasks.update_one(owner_filter(task), {
            "$set": update,
            "$unset": {"plannedRestart": "", "plannedRestartNodeId": "", "plannedRestartToken": ""},
        })
        await self.repo.db.tasks.find_one_and_update(
            {**owner_filter(task), "nodeId": None, "status": update["status"], "restartRequested": True},
            {"$set": {"desiredState": "RUNNING", "restartRequested": False}}, return_document=ReturnDocument.AFTER)
        self.discard_closed(runtime)

    async def isolate_active_sessions(self):
        """数据库长期失联时逐一关闭本机连接，不凭异常假定端点已经释放。

        成功停止的实例才从 active 移除；停止失败的实例保留，后续人工或恢复后的
        周期可继续隔离，端点锁也不会被本节点错误删除。
        """
        from camera_logs.node.failover import self_fence_active_sessions
        await self_fence_active_sessions(self)

    async def stop_unresponsive_health_failed_runtimes(self, assigned, current_monotonic):
        """用本地接收证据延后认证失败停止，避免 HTTP 探测盖过真实设备日志。

        资源健康状态、运行归属和系统恢复标记都由事务 helper 再读再核对；这里不做
        HTTP 请求，也不按每行写入数据库。TELNET_SERIAL 从运行时资格层直接排除。
        """
        from camera_logs.resources.health_policy import stop_unresponsive_task

        for task_id, runtime in list(self.active.items()):
            task = assigned.get(task_id)
            if task is None or owner_filter(runtime.task) != owner_filter(task):
                continue
            liveness = getattr(runtime, "liveness", None)
            eligible = getattr(liveness, "allows_health_stop", None)

            def liveness_check(instant, eligible=eligible, task=runtime.task, runtime=runtime):
                """未知运行时替身没有本机接收状态时保持保守，不发起系统停止。"""
                return callable(eligible) and eligible(
                    task, getattr(runtime, "collector", None), stopping=getattr(runtime, "stopping", False),
                    retired=getattr(runtime, "retired", False), current_monotonic=instant,
                )
            if liveness_check(current_monotonic):
                stopped = await stop_unresponsive_task(
                    self.repo, task, liveness_check=lambda: liveness_check(time.monotonic()),
                )
                if stopped:
                    # 同一 tick 后续仍使用该快照；立即反映事务已提交的停止意图，不能
                    # 再领取手动命令或沿用 RUNNING 分支。
                    task["desiredState"] = "STOPPED"

    async def tick(self):
        """更新资源心跳、处理期望状态并分发作业，不在本周期等待耗时归档。"""
        from camera_logs.collection.connections import connect
        from camera_logs.collection.ssh_admission import SshAdmission

        async def direct_connect(config):
            """每次实际SSH建连独立占位，任务副本中的内部对象不入库或发送设备。"""
            if config.get("protocol") == "SSH":
                config = dict(config) | {"_sshAdmission": SshAdmission(self.repo, config)}
            return await connect(config)

        async def admitted_connect(config):
            """主机保持原端口直连；仅从机进入共享扩展端口和嵌套SSH握手。"""
            if config.get("protocol") == "SSH" and config.get("sshTarget", "HOST") != "HOST":
                from camera_logs.collection.slave_ssh import connect_slave
                return await connect_slave(self, config, direct_connect)
            return await direct_connect(config)
        root = self.repo.settings.log_root
        root.mkdir(parents=True, exist_ok=True)
        self.telemetry.start_if_idle()
        # NFS 扫描与采集会话完全分离，且最多一个后台任务；扫描慢不能阻塞心跳或写入。
        if self.coredump_scanner is None:
            from camera_logs.coredumps.scanner import CoredumpScanner
            self.coredump_scanner = CoredumpScanner(self.repo)
        interval = float(self.repo.settings.coredump_scan_interval_seconds)
        if (self.coredump_scan_task is None or self.coredump_scan_task.done()) and time.monotonic() - self.last_coredump_scan >= interval:
            self.last_coredump_scan = time.monotonic()
            self.coredump_scan_task = asyncio.create_task(self.coredump_scanner.scan_once())
            self.coredump_scan_task.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
        disk = shutil.disk_usage(root)
        disk_percent = disk.used / disk.total * 100
        await self.report_disk_pressure(disk_percent)
        # 管理员登记的节点容量是运行时权威配置；环境变量只为首次未登记节点
        # 提供兜底值。不能用较小的环境默认值静默截断已保存的后台配置。
        config = await self.repo.db.node_configs.find_one({"id": self.repo.settings.node_id}) or {}
        capacity = config.get("capacity", self.repo.settings.node_capacity)
        mismatch = bool(config.get("url") and config["url"].rstrip("/") != self.repo.settings.node_url.rstrip("/"))
        reported = await self.repo.db.nodes.find_one({"id": self.repo.settings.node_id}) or {}
        write_latency = self.write_latency()
        from camera_logs.node.performance_runtime import update_performance
        performance_status = await update_performance(self, config, write_latency, disk_percent)
        await self.write_pressure.report(self.repo.db, self.repo.settings.node_id, write_latency, config)
        accepting = (
            disk_percent < 90
            and config.get("accepting", True)
            and not config.get("deletedAt")
            and not reported.get("deletedAt")
            and not mismatch
            and not reported.get("isolated", False)
            and not write_latency_blocked(write_latency, config)
            and not resource_pressure({"telemetry": self.telemetry.value})
            and not performance_status.get("admissionBlocked")
        )
        rate = self.input_rate.sample(self.active.values())
        # 管理配置每周期重读；已分配但尚未建连的任务也必须等吞吐恢复。
        accepting = accepting and not input_rate_blocked({"inputBytesPerSecond": rate}, config)
        await self.repo.db.nodes.update_one({"id": self.repo.settings.node_id}, {"$set": {
            "id": self.repo.settings.node_id, "url": self.repo.settings.node_url, "workerReportedAt": now(),
            "telemetryAgeSeconds": self.telemetry.age_seconds(),
            "capacity": capacity, "activeTasks": len(self.active),
            "performanceStatus": performance_status,
            "inputRateLimitMiB": input_rate_limit(config),
            "writeLatencyLimitMs": write_latency_limit(config),
            "diskPercent": disk_percent, "diskFreeBytes": disk.free, "inputBytesPerSecond": rate,
            **write_latency,
            "configurationMismatch": mismatch,
            "configuredUrl": config.get("url"), "telemetry": self.telemetry.value,
            "capabilities": {"coredumpNfs": bool(self.repo.settings.nfs_server_ip)},
            # 心跳由平台数据库接收时生成，禁止将节点墙上时间作为在线依据。
            "accepting": accepting}, "$currentDate": {"heartbeat": True},
            "$unset": {"shuttingDownAt": ""}}, upsert=True)
        from camera_logs.node.failover import persist_self_fenced_receipts, retry_self_fence_pending
        await persist_self_fenced_receipts(self)
        for task_id, future in list(self.releases.items()):
            if future.done():
                try:
                    future.result()
                except Exception:
                    logger.exception("运行实例释放失败 task=%s", task_id)
                self.releases.pop(task_id, None)
        # 已完成的失败自围栏先移出 releases，再由专属流程重试；不能在本周期
        # 让普通 isolate/release 看见该运行并改写其关闭语义。
        await retry_self_fence_pending(self)
        tasks = [t async for t in self.repo.db.tasks.find({"nodeId": self.repo.settings.node_id})]
        # 心跳写入和完整归属读取均成功才刷新控制面存活；命令/导出慢查询
        # 不应把已成功核对归属的节点误判成数据库长期失联。
        self.last_database_ok = time.monotonic()
        assigned = {task["id"]: task for task in tasks}
        # 仅成功取得数据库快照后核对归属；读取失败不能被解释成任务已经消失。
        await self.stop_unresponsive_health_failed_runtimes(assigned, time.monotonic())
        for task_id, runtime in list(self.active.items()):
            current = assigned.get(task_id)
            retry = self.failed_cleanups.get(task_id)
            if (
                task_id not in self.releases
                and retry is not None
                and retry[0] is runtime
                and current is not None
                and owner_filter(runtime.task) == owner_filter(current)
                and current["status"] == "BLOCKED"
                and not reported.get("isolated", False)
            ):
                self.releases[task_id] = asyncio.create_task(self.retry_blocked_cleanup(runtime))
                continue
            if task_id not in self.releases and current is not None and current["status"] == "BLOCKED" \
                    and owner_filter(runtime.task) == owner_filter(current) and (current.get("restartRequested") \
                    or current.get("desiredState") == "STOPPED") and not reported.get("isolated", False):
                # 本机仍持有同一运行时，stop 成功就是关闭证据；普通停止和受控重启都可收尾。
                action = "recover" if current.get("restartRequested") else "release"
                self.releases[task_id] = asyncio.create_task(self.finish_runtime(runtime, action))
                continue
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
                if runtime is None:
                    if await finalize_closed_task(self.repo, task):
                        continue
                    await self.repo.db.operations.update_many(
                        {"taskId": task["id"], "status": "PENDING", "$or": [
                            {"action": "restart-blocked"}, {"desiredState": "STOPPED"},
                        ]},
                        {"$set": {"phase": "ISOLATION_REQUIRED", "updatedAt": now()}},
                    )
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
                self.active[task["id"]] = SessionRuntime(self.repo, task, admitted_connect)
            elif runtime is None and task["status"] not in ("STOPPED", "BLOCKED"):
                await self.repo.db.tasks.update_one(owner_filter(task),
                    {"$set": {"status": "BLOCKED", "error": "运行实例已丢失，等待隔离确认"}})
            if runtime and not runtime.stopping and task["status"] == "COLLECTING":
                manual = self.manual_jobs.get(task["id"])
                if manual is None or manual.done():
                    self.manual_jobs[task["id"]] = asyncio.create_task(self.dispatch_manual(runtime))
        self.jobs = {job for job in self.jobs if not job.done()}
        if len(self.jobs) < 2:
            from camera_logs.logs.job_lease import claim_job
            job = await claim_job(self.repo, self.instance_id)
            if job:
                from camera_logs.logs.job_execution import run_leased_job
                self.track_background(asyncio.create_task(run_leased_job(self.repo, job)), "日志查询与导出")
        if len(self.jobs) < 2:
            export = await self.repo.db.coredump_exports.find_one_and_update(
                {"coordinatorNodeId": self.repo.settings.node_id, "status": "QUEUED"},
                {"$set": {"status": "RUNNING", "startedAt": now(), "workerInstanceId": self.instance_id,
                          "leaseUntil": now() + timedelta(seconds=90)}}, return_document=ReturnDocument.AFTER)
            if export:
                from camera_logs.coredumps.jobs import run_export
                self.track_background(asyncio.create_task(run_export(self.repo, export)), "coredump 导出")
        if time.monotonic() - self.last_maintenance >= 60:
            self.last_maintenance = time.monotonic()
            if self.maintenance_task is None or self.maintenance_task.done():
                self.maintenance_task = asyncio.create_task(self._maintain())

    async def dispatch_manual(self, runtime):
        """单任务查询与发送共用一个作业；慢查询不阻塞监督、停止或其它任务。"""
        try:
            async with self.manual_query_slots:
                async with asyncio.timeout(3):
                    command = await next_manual_command(self.repo, runtime)
            if command and self.active.get(runtime.task["id"]) is runtime \
                    and not runtime.stopping and not getattr(runtime, "retired", False):
                await runtime.manual(command)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            logger.warning("手动命令查询超时，下轮重试 task=%s", runtime.task["id"])
        except Exception:
            logger.exception("手动命令查询或执行失败 task=%s", runtime.task["id"])

    async def _maintain(self):
        """执行低频清理；无 NFS 功能节点保留既有快照且跳过其专属回收。"""
        from camera_logs.coredumps.jobs import cleanup_expired_exports
        from camera_logs.logs.job_lease import recover_expired_jobs
        from camera_logs.logs.maintenance import maintain

        await recover_expired_jobs(self.repo)
        await maintain(self.repo)
        await cleanup_expired_exports(self.repo)
        from camera_logs.logs.search_index_maintenance import backfill_search_indexes
        from camera_logs.node.performance_runtime import background_settings
        if background_settings()[1]:
            await backfill_search_indexes(self.repo)
        if not self.repo.settings.nfs_server_ip:
            return
        from camera_logs.coredumps.snapshots import reconcile_snapshots
        await reconcile_snapshots(self.repo)

    async def run(self):
        """持续执行节点周期；长期失去数据库联系时主动关闭本机连接。"""
        while True:
            try:
                await asyncio.wait_for(self.tick(), timeout=10)
            except asyncio.CancelledError:
                raise
            except Exception:
                control_age = time.monotonic() - self.last_database_ok
                logger.exception("节点周期失败 node=%s control_snapshot_age_seconds=%.3f active_tasks=%d",
                                 self.repo.settings.node_id, control_age, len(self.active))
                if control_age > 15:
                    logger.error("控制面归属超过15秒未确认，主动关闭本节点采集连接 node=%s active_tasks=%d",
                                 self.repo.settings.node_id, len(self.active))
                    await self.isolate_active_sessions()
            await asyncio.sleep(1)

    async def close(self):
        """进程退出时回收本机连接，并保留已确认的任务控制意图。"""
        try:
            await mark_unavailable_for_shutdown(self)
        except Exception:
            logger.exception("节点关闭前未能暂停新任务准入")
            allow_release = False
        else:
            allow_release = True
        await shutdown_runtimes(self, allow_release=allow_release)
        # 只有本 Worker 的所有运行和收尾都完成后，新的 Worker 才能消费计划重启
        # 标记。超时或异常退出不会写完成标记，旧归属继续走隔离证明流程。
        if allow_release and not self.active and not self.releases:
            await self.repo.db.nodes.update_one(
                {"id": self.repo.settings.node_id, "shutdownToken": self.instance_id, "accepting": False},
                {"$set": {"shutdownCompletedAt": now()}},
            )
        for job in self.jobs | set(self.manual_jobs.values()):
            job.cancel()
        tasks = [*self.jobs, *self.manual_jobs.values()]
        if self.maintenance_task:
            self.maintenance_task.cancel()
            tasks.append(self.maintenance_task)
        if self.coredump_scan_task:
            self.coredump_scan_task.cancel()
            tasks.append(self.coredump_scan_task)
        await self.telemetry.close()
        await asyncio.gather(*tasks, return_exceptions=True)


def create_worker_app(settings=None):
    """兼容原节点入口；内部服务装配已迁至独立 app 模块。"""
    from camera_logs.node.app import create_worker_app as build_worker_app
    return build_worker_app(settings)


if __name__ == "__main__":
    import os

    import uvicorn
    uvicorn.run(create_worker_app(), host="0.0.0.0", port=int(os.getenv("NODE_PORT", "18081")))
