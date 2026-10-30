"""把设备会话接入任务运行、日志目录和持久化命令预算。

同一次运行可以包含多个重连会话，预算按运行 ID 和命令 ID 累计；实时缓冲有界，
原始日志由存储层完整保存。异常重连创建新会话，不能复用旧会话手动命令。
"""
import asyncio
import base64
import hashlib
import logging
import re
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pymongo.errors import ConnectionFailure, DuplicateKeyError, ExecutionTimeout, PyMongoError

from camera_logs.collection.collector import Collector, CommandChannelBlocked
from camera_logs.collection.coredump_lease import (
    guard_coredump_monitor,
    record_coredump_status,
)
from camera_logs.collection.liveness import RuntimeLiveness
from camera_logs.collection.output_recovery import claim_output_recovery_lease
from camera_logs.collection.psh_dialogue import PshSwitchError
from camera_logs.collection.psh_passwords import PshPasswordProvider
from camera_logs.collection.resource_auth_gate import wait_for_resource_authentication
from camera_logs.collection.runtime_events import (
    apply_runtime_state,
    record_debug_event,
    record_runtime_event,
)
from camera_logs.collection.runtime_monitors import (
    monitor_session,
    release_monitor_leases,
    stop_session_monitors,
)
from camera_logs.collection.slave_shell import SlaveLoginError
from camera_logs.collection.ssh_admission import SshCapacityError, SshSlotUncertain
from camera_logs.commands.manual_claim import ManualClaimUncertain, claim_manual
from camera_logs.commands.reservation import ReservationUncertain, reserve_scheduled
from camera_logs.common.database import now
from camera_logs.common.log_frames import DataFrame
from camera_logs.common.ownership import OwnershipLost, owner_filter
from camera_logs.common.resource_monitoring import supports_resource_monitoring
from camera_logs.logs.catalog_identity import file_identity
from camera_logs.logs.segment_size import segment_size_cache

logger = logging.getLogger(__name__)


class SessionRuntime:
    """节点独占的单任务运行，负责状态回写、重连和持久化执行记录。"""
    def __init__(self, repo, task, connection_factory):
        self.repo, self.task, self.factory = repo, dict(task), connection_factory
        self.collector = None
        self.stopping = False
        self.retired = False
        self.frames = deque()
        self.frame_bytes = 0
        self.frame_number = 0
        self.tail_changed = asyncio.Condition()
        self.paths = {}
        self.catalog_dirty = {}
        self.catalog_ready = set()
        self.catalog_lock = asyncio.Lock()
        self.last_catalog = 0.
        self.pending_executions = {}
        self.input_bytes = 0
        self.error = None
        self.connection_uncertain = None
        self.liveness = RuntimeLiveness()
        self.resource_monitor_task = None
        self.last_already_ash = None
        self.last_already_ash_event = None
        self.preconnect_slot_reconciled = False
        self.started_at = now()
        self.debug_passwords = PshPasswordProvider(repo.settings)
        # 目录水位不能只依赖下一块日志触发；设备停止输出后的尾块同样需要发布。
        self.catalog_task = asyncio.create_task(self._catalog_loop())
        self.background = asyncio.create_task(self.run())

    async def on_debug(self, event, details):
        """委托事件模块保存调试阶段及会话级确认去重，不保存密文或口令。"""
        await record_debug_event(self, event, details)

    async def on_coredump(self, status, error):
        """单独登记挂载事件，不覆盖采集状态或把敏感挂载命令写入审计。"""
        await record_coredump_status(self.repo, self.task, self.collector, status, error, logger)

    async def coredump_guard(self):
        """在每次可选设备控制前续租资源，并确认本运行仍拥有任务及健康资源。"""
        result = await guard_coredump_monitor(
            self.repo,
            self.task,
            session_active=lambda: not (self.stopping or self.retired or self.collector is None
                                        or self.collector._closed.is_set()),
            report_status=self.on_coredump,
            mount_target=getattr(self.collector, "_coredump_target", None),
        )
        if result is None and self.collector is not None:
            winner = await self.coredump_target()
            if winner:
                self.collector._coredump_target = winner
        return result

    async def coredump_target(self):
        """接管会话读取资源中已固定的设备侧 NFS 来源，避免覆盖旧导出。"""
        resource = await self.repo.db.resources.find_one(
            {"id": self.task.get("resourceId")}, {"coredumpLeaseTarget": 1},
        )
        return resource.get("coredumpLeaseTarget") if resource else None

    def file_id(self, path):
        """以不可变原始分卷路径生成逻辑 ID，多个成员可共享一个小时归档。"""
        return file_identity(self.repo.settings.log_root, Path(path), self.repo.settings.node_id)

    async def on_log(self, chunk):
        """发布带字节范围的实时帧并周期更新目录，慢浏览器不阻塞原始存储。"""
        # 一个批次可以跨整点，必须使用块上绑定的路径，不能读取写入器当前文件。
        path = Path(chunk.path)
        file_id = self.file_id(path)
        previous = self.paths.get(file_id, chunk.offset)
        end_offset = chunk.offset + len(chunk.data)
        self.paths[file_id] = max(previous, end_offset)
        if file_id not in self.catalog_ready:
            self.catalog_dirty[file_id] = {"id": file_id, "taskId": chunk.task_id, "runId": chunk.run_id,
                "sessionId": chunk.session_id, "nodeId": self.repo.settings.node_id,
                "hour": self._hour_from_path(path), "path": str(path), "bytes": self.paths[file_id],
                "runStartedAt": self.task.get("runStartedAt", self.started_at), "rawFileName": path.name,
                "indexPath": str(path.with_suffix(".index.jsonl")), "segmentNumber": self._segment_number(path),
                "sessionStartedAt": self.started_at, "firstSequence": chunk.sequence, "status": "OPEN"}
        self.input_bytes += len(chunk.data)
        self.frame_number += 1
        frame = DataFrame(
            type="data", fileId=file_id, sessionId=chunk.session_id, runId=chunk.run_id,
            offset=chunk.offset, endOffset=end_offset, cursor=f'{self.task["runId"]}:{self.frame_number}',
            data=base64.b64encode(chunk.data).decode(), size=len(chunk.data),
        ).model_dump(mode="json")
        self.frames.append(frame)
        self.frame_bytes += len(chunk.data)
        while self.frame_bytes > 8*1024*1024 and self.frames:
            self.frame_bytes -= self.frames.popleft()["size"]
        # 只在已追加的内存帧后同步唤醒内部 tail；不创建逐块后台任务，
        # 等待者会在同一事件循环内重新按自己的游标读取缓冲。
        async with self.tail_changed:
            self.tail_changed.notify_all()
        if previous == 0 or time.monotonic()-self.last_catalog >= 1:
            await self._publish_catalog()

    async def _publish_catalog(self):
        """发布全部脏水位；失败不移除内存项，下一周期继续尝试。"""
        async with self.catalog_lock:
            dirty = list(self.catalog_dirty.items())
            for file_id, entry in dirty:
                # 重新读取当前水位，避免快照把同一文件的后续字节写小。
                value = entry | {"updatedAt": now()}
                set_on_insert = {"createdAt": now()}
                if entry["status"] == "OPEN":
                    value["bytes"] = self.paths.get(file_id, entry["bytes"])
                    value.pop("firstSequence")
                    set_on_insert["firstSequence"] = entry["firstSequence"]
                else:
                    # 已存在的 OPEN 文档持有首次会话时间；归档不能用当前运行实例
                    # 的时间重写它。首次直接收到 READY 时才由插入路径提供这些值。
                    set_on_insert["runStartedAt"] = value.pop("runStartedAt")
                    set_on_insert["sessionStartedAt"] = value.pop("sessionStartedAt")
                query = {"id": file_id}
                if entry["status"] == "OPEN":
                    # 独立目录恢复可能先完成READY；迟到实时水位绝不能重新打开已归档分卷。
                    query["status"] = {"$in": ["OPEN", None]}
                try:
                    await self.repo.db.files.update_one(query, {"$set": value,
                        "$setOnInsert": set_on_insert}, upsert=True)
                except DuplicateKeyError:
                    existing = await self.repo.db.files.find_one({"id": file_id})
                    if (entry["status"] != "OPEN" or existing is None
                            or existing.get("status") not in {"READY", "DELETING"}
                            or any(existing.get(key) != entry.get(key) for key in
                                   ("taskId", "runId", "sessionId", "nodeId"))):
                        raise
                    self.catalog_ready.add(file_id)
                # update_one 可让出事件循环；仅消费仍是本次快照的条目，不能丢掉
                # 期间收到的后续日志水位或归档最终状态。
                if self.catalog_dirty.get(file_id) is entry:
                    self.catalog_dirty.pop(file_id, None)
            self.last_catalog = time.monotonic()

    async def _catalog_loop(self):
        """每秒独立冲刷目录尾部，不依赖设备持续输出。"""
        while not self.stopping:
            await asyncio.sleep(1)
            if self.catalog_dirty:
                try:
                    await self._publish_catalog()
                except Exception:
                    # 脏水位保留到下一周期重试。
                    logger.exception("日志目录水位发布失败 task=%s", self.task["id"])

    @staticmethod
    def _segment_number(path):
        """从固定宽度分卷名恢复小时内顺序；旧片段没有分卷号。"""
        match = re.search(r"part-(\d+)\.log$", Path(path).name)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _hour_from_path(path):
        """把上海自然小时目录转换为数据库统一使用的 UTC 时间文本。"""
        try:
            parent = Path(path).parent
            zone = ZoneInfo("Asia/Shanghai")
            try:
                year, month, day = (int(value) for value in parent.parent.name.split("-"))
                local = datetime(year, month, day, int(parent.name), tzinfo=zone)
            except ValueError:
                year, month, day, hour = (int(value) for value in parent.parts[-4:])
                local = datetime(year, month, day, hour, tzinfo=zone)
            return local.astimezone(UTC).isoformat()
        except (TypeError, ValueError):
            return now().astimezone(UTC).isoformat()

    async def on_archive(self, archive):
        """仅在压缩校验和原子发布成功后把目录记录设为可下载。"""
        path = archive.path
        archive_bytes = path.stat().st_size
        log_path = getattr(archive, "log_path", None) or path
        file_id = self.file_id(log_path)
        member_name = getattr(archive, "member_name", None)
        index_path = getattr(archive, "index_path", None)
        member_fields = {}
        if member_name:
            member_fields = {"archiveMember": member_name, "rawFileName": member_name,
                "archiveGroupId": hashlib.sha256(str(path.relative_to(self.repo.settings.log_root)).encode()).hexdigest(),
                "segmentNumber": self._segment_number(log_path)}
            for source, target in (("first_received_at", "firstReceivedAt"),
                                   ("last_received_at", "lastReceivedAt")):
                value = getattr(archive, source, None)
                if value is not None:
                    member_fields[target] = value.astimezone(UTC).isoformat()
        if index_path:
            member_fields["indexPath"] = str(index_path)
        async with self.catalog_lock:
            # archive 是该不可变分卷的最终状态；清掉脏 OPEN 快照后再发布 READY，
            # 周期任务不会用最后实时水位把 READY 覆盖回 OPEN。
            self.catalog_dirty.pop(file_id, None)
            self.catalog_ready.add(file_id)
            self.catalog_dirty[file_id] = {"id": file_id, "taskId": archive.task_id, "runId": archive.run_id,
                "sessionId": archive.session_id, "nodeId": self.repo.settings.node_id,
                "hour": archive.hour_start.astimezone(UTC).isoformat(), "path": str(path),
                "archiveName": path.name, "bytes": archive.raw_size, "archiveBytes": archive_bytes,
                "sha256": archive.sha256, "firstSequence": archive.first_sequence,
                "lastSequence": archive.last_sequence, "status": "READY", **member_fields,
                "runStartedAt": self.task.get("runStartedAt", self.started_at),
                "sessionStartedAt": self.started_at}
        # 同一小时包原子替换后会包含此前 session 的成员；刷新既有 READY 记录的
        # 目录统计，避免成员仍指向同一 tar 却报告发布前的旧压缩大小。
        await self.repo.db.files.update_many(
            {"path": str(path), "status": "READY"}, {"$set": {"archiveBytes": archive_bytes}},
        )
        # READY 也由统一发布器落库。失败时条目保留，下一周期不会遗漏已删原始正文。
        await self._publish_catalog()

    async def on_state(self, state, details):
        """委托状态事件模块处理归属核验、审计事件与任务状态映射。"""
        collector = getattr(self, "collector", None)
        if collector is not None and details.get("sessionId") == collector.session_id:
            liveness = getattr(self, "liveness", None)
            if liveness is not None:
                liveness.state_changed(state, collector, time.monotonic())
        await apply_runtime_state(self, state, details)

    async def reserve(self, command_id, details):
        """复核发送会话及运行归属后占用预算；不确定结果不退回次数。"""
        collector = self.collector
        session_id = details.get("sessionId")
        if (self.stopping or getattr(self, "retired", False) or collector is None
                or session_id != collector.session_id or details.get("taskId") != self.task["id"]
                or details.get("runId") != self.task["runId"]):
            return False
        # 多数确认完成前禁止发送；取消或提交未知不退回预算，也不补建执行记录。
        try:
            execution = await reserve_scheduled(self.repo, self.task, command_id, session_id)
        except ReservationUncertain as error:
            # 发送器捕获连接类异常并按固定 ID 写 UNKNOWN，实际未提交时更新为空操作。
            self.pending_executions[(session_id, command_id)] = error.execution_id
            logger.exception("定时命令预留未确认 task=%s command=%s", self.task["id"], command_id)
            raise
        if execution is None:
            return False
        self.pending_executions[(session_id, command_id)] = execution["id"]
        return True

    async def update_execution(self, command_id, status, details):
        """只更新原会话仍在发送的记录，迟到回调不覆盖 UNKNOWN 或后继执行。"""
        if details.get("taskId") != self.task["id"] or details.get("runId") != self.task["runId"]:
            return
        session_id = details.get("sessionId")
        key = (session_id, command_id)
        identifier = self.pending_executions.get(key)
        if identifier:
            completed_at = now()
            for attempt in range(3):
                try:
                    # 只重试幂等的结果更新，绝不重新入队发送；记录实际处理结束时间。
                    await self.repo.db.commands.update_one({"id": identifier, "taskId": self.task["id"],
                        "runId": self.task["runId"], "sessionId": session_id, "commandId": command_id,
                        "kind": "SCHEDULED", "status": "SENDING"},
                        {"$set": {"status": status, "completedAt": completed_at}})
                    break
                except (ConnectionFailure, ExecutionTimeout):
                    logger.exception("定时命令结果回写失败，等待重试 task=%s session=%s command=%s",
                                     self.task["id"], session_id, command_id)
                    if (self.stopping or getattr(self, "retired", False) or self.collector is None
                            or self.collector.session_id != session_id):
                        return
                    if attempt == 2:
                        raise
                    # 三次失败交给会话监督收尾；只在前两次失败后等待 1 秒、2 秒。
                    await asyncio.sleep(2 ** attempt)
                    if (self.stopping or getattr(self, "retired", False) or self.collector is None
                            or self.collector.session_id != session_id):
                        return
            if self.pending_executions.get(key) == identifier:
                self.pending_executions.pop(key, None)

    async def manual(self, document):
        """只在命令绑定的会话发送；已过期会话取消，进入发送后异常记为未知。"""
        collector = self.collector
        if self.stopping or getattr(self, "retired", False) or collector is None:
            return
        # 先读取命令再核对当前归属，防止旧快照取消已属于后继实例的记录。
        record = await self.repo.db.commands.find_one({"id": document["id"], "taskId": self.task["id"],
                                                     "kind": "MANUAL", "status": "QUEUED"})
        if record is None:
            return
        scope = {"id": record["id"], "taskId": self.task["id"], "kind": "MANUAL",
                 "runId": record.get("runId"), "sessionId": record.get("sessionId")}
        try:
            claimed = await claim_manual(self.repo, self.task, collector.session_id, record)
        except ManualClaimUncertain:
            # 事务可能已经把记录改为 SENDING，但未曾调用 socket；只向前推进到 UNKNOWN。
            logger.exception("手动命令领取结果未知 task=%s command=%s", self.task["id"], record["id"])
            try:
                await self.repo.db.commands.update_one({**scope, "status": "SENDING"},
                    {"$set": {"status": "UNKNOWN", "error": "命令领取状态未知，未发送", "completedAt": now()}})
            except PyMongoError:
                logger.exception("手动命令未知领取状态无法回写 task=%s command=%s", self.task["id"], record["id"])
            return
        if not claimed:
            return
        async def session_guard():
            """在 collector 真正轮到写入前复核本地实例和数据库归属。"""
            if (self.stopping or getattr(self, "retired", False) or self.collector is not collector
                    or collector.session_id != claimed["sessionId"]):
                raise OwnershipLost("手动命令发送前会话已失属")
            current = await self.repo.db.tasks.find_one({**owner_filter(self.task), "sessionId": collector.session_id,
                "status": "COLLECTING", "desiredState": "RUNNING", "deletedAt": None,
                "resourceDeleted": {"$ne": True}})
            # 查询 await 期间本地运行可能已停止或 collector 已被替换，返回后必须再核对。
            if (current is None or self.stopping or getattr(self, "retired", False)
                    or self.collector is not collector or collector.session_id != claimed["sessionId"]):
                raise OwnershipLost("手动命令发送前任务归属已变化")
        status = "UNKNOWN"
        error = None
        try:
            # 快检缩短已失属命令在内存队列中的停留；collector 在实际写入前仍会再调用守卫。
            await session_guard()
            # collector 内部负责提示符等待及发送后延时，外层不能提前取消其串行收尾。
            await collector.enqueue_manual(
                claimed["command"],
                newline=claimed.get("newline", "\n"),
                prompt=claimed.get("prompt"),
                timeout_seconds=float(claimed.get("timeoutSeconds", 30)),
                delay_seconds=float(claimed.get("delaySeconds", 0)),
                session_guard=session_guard,
            )
            status = "SENT"
        except OwnershipLost:
            # session_guard 在真正写 socket 前抛出，明确知道业务命令尚未开始发送。
            status = "CANCELLED"
        except PyMongoError:
            logger.exception("手动命令归属复核失败 task=%s command=%s", self.task["id"], record["id"])
            error = "命令归属复核失败，未发送"
        except CommandChannelBlocked as exc:
            status = "FAILED"
            error = str(exc)
        except PshSwitchError:
            status = "FAILED"
            error = "PSH 调试失败，本次命令未自动重试"
        except Exception:
            logger.exception("手动命令结果未知 task=%s command=%s", self.task["id"], document["id"])
        finally:
            await self.repo.db.commands.update_one({**scope, "status": "SENDING"},
                {"$set": {"status": status, "error": error, "completedAt": now()}})

    async def _consume_reconnect_wake(self):
        """只消费本运行仍在退避的认证恢复标记，旧会话不得清除后继请求。"""
        if self.task.get("protocol") != "SSH":
            return False
        try:
            async with asyncio.timeout(2):
                current = await self.repo.db.tasks.find_one({
                    **owner_filter(self.task), "desiredState": "RUNNING", "status": "RECONNECTING",
                    "reconnectWake": {"$exists": True},
                }, {"reconnectWake": 1})
                if current is None:
                    return False
                changed = await self.repo.db.tasks.update_one({
                    **owner_filter(self.task), "desiredState": "RUNNING", "status": "RECONNECTING",
                    "reconnectWake": current["reconnectWake"],
                }, {"$unset": {"reconnectWake": ""}})
            return bool(changed.matched_count)
        except (PyMongoError, TimeoutError):
            # 认证恢复只是缩短既有等待；数据库暂不可用时保留原有重连节奏。
            logger.exception("认证恢复唤醒查询失败 task=%s", self.task["id"])
            return False

    async def _wait_reconnect(self, seconds):
        """等待重连退避，认证成功可唤醒当前 SSH 运行，停止/失属仍由原收尾流程处理。"""
        deadline = time.monotonic() + seconds
        while not self.stopping:
            if await self._consume_reconnect_wake():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            # 无跨进程内存信号时短轮询持久化标记；只在失败退避窗口执行。
            await asyncio.sleep(min(1, remaining))

    async def run(self):
        """连接失败指数退避，先释放旧连接再重试；每次登录后重新发送初始化命令。"""
        delay = 1
        while not self.stopping:
            session_commands = None
            fixed_slave_retry = False
            try:
                if not await wait_for_resource_authentication(self.repo, self.task, stopping=lambda: self.stopping):
                    raise OwnershipLost("重连前资源认证、归属或运行意图已失效")
                config = dict(self.task)
                resource = await self.repo.db.resources.find_one(
                    {"id": config.get("resourceId"), "deletedAt": None}, {"kind": 1},
                )
                if resource:
                    # 串口传输可能连向海康设备或普通串口服务器；输出控制只接受这里的
                    # 资源快照，不能从协议名或端口猜测设备能力。
                    config["resourceKind"] = resource.get("kind")
                config["password"] = self.repo.decrypt(config["passwordEncrypted"])
                config["knownHosts"] = self.repo.settings.known_hosts
                config["verifyHostKey"] = self.repo.settings.ssh_verify_host_key
                config["pshSerialCharacterInterval"] = self.repo.settings.psh_serial_character_interval
                self.started_at = now()
                reset = {"shellMode": "UNKNOWN", "debugPhase": None, "commandBlocked": False, "debugError": None,
                         "effectiveSshPort": config.get("port") if config.get("protocol") == "SSH" and config.get("sshTarget", "HOST") == "HOST" else None,
                         "slaveConnectedAt": None,
                         "coredumpMountStatus": None, "coredumpMountError": None, "coredumpMountRunId": None,
                         "coredumpCheckedAt": None}
                admitted = await self.repo.db.tasks.update_one(
                    {**owner_filter(self.task), "status": {"$ne": "BLOCKED"}},
                    {"$set": reset, "$unset": {"reconnectWake": ""}})
                if not admitted.matched_count:
                    raise OwnershipLost("重连前任务归属或准入已失效")
                self.collector = Collector(config, self.repo.settings.log_root, connection_factory=self.factory,
                    on_log=self.on_log, on_state=self.on_state, on_archive=self.on_archive,
                    reserve_execution=self.reserve, update_execution=self.update_execution,
                    resolve_debug_password=self.debug_passwords, on_debug=self.on_debug,
                    claim_output_recovery=lambda: claim_output_recovery_lease(self.repo, self.task),
                    segment_bytes=segment_size_cache(self.repo).current)
                collector = self.collector
                collector._on_receive = lambda collector=collector: (
                    self.liveness.received(collector) if collector is self.collector else None
                )
                collector._on_liveness_reset = lambda instant, collector=collector: (
                    self.liveness.observation_reset(instant) if collector is self.collector else None
                )
                # 收尾条件绑定本次实际创建的会话，旧运行不能取消后继会话的命令。
                session_commands = {"taskId": self.task["id"], "runId": self.task["runId"],
                                    "sessionId": self.collector.session_id}
                await self.collector.start()
                if supports_resource_monitoring(config):
                    self.resource_monitor_task = asyncio.create_task(monitor_session(self, self.collector))
                delay = 1
                await self.collector.wait_closed()
                if self.task.get("protocol") == "SSH" and self.task.get("sshTarget", "HOST") != "HOST":
                    # 从机内层 EOF 与握手/切换错误一样等待十秒，避免主机切换期快速重试。
                    delay = 10
                    fixed_slave_retry = True
            except asyncio.CancelledError:
                raise
            except OwnershipLost:
                self.retired = self.stopping = True
                logger.info("运行实例停止重连，归属或准入已失效 task=%s", self.task["id"])
                break
            except PshSwitchError as exc:
                if self.task.get("protocol") != "SSH" or self.task.get("sshTarget", "HOST") == "HOST":
                    self.error = str(exc)
                    break
                delay = 10
                fixed_slave_retry = True
                logger.info("从机调试切换失败，十秒后重试 task=%s error=%s", self.task["id"], exc)
            except SshSlotUncertain as exc:
                # 只保存不带栈的身份快照；反复抛同一异常会累加历史帧并耗尽日志轮转。
                logger.exception("SSH名额结果未知，保持隔离 task=%s", self.task["id"])
                self.connection_uncertain = SshSlotUncertain(
                    exc.address, exc.token, preconnect=exc.preconnect,
                    admission_epoch=exc.admission_epoch,
                )
                self.error = str(exc)
                break
            except SshCapacityError as exc:
                await self.repo.db.tasks.update_one(owner_filter(self.task), {"$set": {"error": str(exc)}})
                logger.info("等待设备SSH连接名额 task=%s", self.task["id"])
            except SlaveLoginError as exc:
                # 从机登录/切换失败通常需要等待主机或设备端状态收敛；固定十秒避免快速重试。
                self.error = None
                delay = 10
                fixed_slave_retry = True
                logger.info("从机连接或切换失败，十秒后重试 task=%s error=%s", self.task["id"], exc)
            except Exception as exc:
                logger.exception("采集会话异常 task=%s", self.task["id"])
                import asyncssh
                if self.task.get("protocol") == "SSH" and self.task.get("sshTarget", "HOST") != "HOST":
                    # 从机工厂的认证、ifconfig、服务引导和连接异常均可能随主机恢复而消失；
                    # 固定十秒避免快速重试。所有权丢失和SSH关闭不确定已在此前分支隔离。
                    delay = 10
                    fixed_slave_retry = True
                    logger.info("从机连接失败，十秒后重试 task=%s error=%s", self.task["id"], type(exc).__name__)
                elif isinstance(exc, asyncssh.PermissionDenied):
                    # 凭据被拒绝不会因重试自行恢复，保留失败状态等待管理员修正账号配置。
                    self.error = "SSH账号认证失败"
                    break
                elif isinstance(exc, asyncssh.HostKeyNotVerifiable):
                    # 指纹缺失或变化必须人工确认，绝不能绕过 known_hosts 自动重连。
                    self.error = "SSH主机指纹未登记或不匹配"
                    break
            finally:
                try:
                    if self.collector:
                        await self._stop_collector()
                finally:
                    await stop_session_monitors(self)
                    await release_monitor_leases(self)
                    # 连接或归档收尾报错仍需结束本会话命令；不能跳过并遗留 SENDING。
                    if session_commands is not None:
                        await self.repo.db.commands.update_many({**session_commands, "status": "QUEUED"},
                            {"$set": {"status": "CANCELLED", "completedAt": now()}})
                        await self.repo.db.commands.update_many({**session_commands, "status": "SENDING"},
                            {"$set": {"status": "UNKNOWN", "completedAt": now()}})
                        for key in list(self.pending_executions):
                            if key[0] == session_commands["sessionId"]:
                                self.pending_executions.pop(key, None)
            if self.error:
                break
            if not self.stopping:
                detected_at = now()
                await record_runtime_event(self, {
                    "taskId": self.task["id"], "runId": self.task["runId"],
                    "sessionId": self.collector.session_id, "nodeId": self.repo.settings.node_id,
                    "type": "CONNECTION_GAP", "detectedAt": detected_at, "createdAt": detected_at,
                    "level": "WARNING", "outcome": "UNKNOWN", "message": "连接中断，设备端未提供补传",
                })
                await self.on_state("RECONNECTING", {"sessionId": self.collector.session_id})
                if fixed_slave_retry:
                    await self._wait_reconnect(10)
                else:
                    import random
                    await self._wait_reconnect(delay + random.random())
                    delay = min(delay*2, 60)

    async def stop(self):
        """禁用重试并等待采集器收尾，连接释放完成后才能取消运行等待。"""
        self.stopping = True
        await self._wake_tail_waiters()
        failure = None
        try:
            if self.collector:
                await self._stop_collector()
        except Exception as error:  # noqa: BLE001 - 收尾后必须把未确认结果交给监督器。
            failure = error
        try:
            if self.catalog_dirty:
                await self._publish_catalog()
        except Exception as error:
            logger.exception("停止时日志目录水位发布失败 task=%s", self.task["id"])
            failure = failure or error
        finally:
            catalog_task = self.catalog_task
            if not catalog_task.done():
                catalog_task.cancel()
                await asyncio.gather(catalog_task, return_exceptions=True)
            if not self.background.done():
                self.background.cancel()
            await asyncio.gather(self.background, return_exceptions=True)
        if failure:
            raise failure
        if getattr(self, "connection_uncertain", None):
            # acquire 阶段尚未返回 socket；只按同一 token 和运行身份精确协调占位。
            # 非预建连异常不能据此推断物理连接不存在，仍由关闭/隔离证明路径处理。
            uncertainty = self.connection_uncertain
            from camera_logs.collection.ssh_admission import reconcile_preconnect_uncertain_slot
            if not uncertainty.preconnect:
                raise SshSlotUncertain(uncertainty.address, uncertainty.token,
                                       admission_epoch=uncertainty.admission_epoch) from None
            await reconcile_preconnect_uncertain_slot(self.repo, self.task, uncertainty)
            self.connection_uncertain = None
            # 工厂在 acquire 前抛出，Collector 没有得到连接对象；协调成功不是关闭收据。
            self.preconnect_slot_reconciled = True
            if self.error == str(uncertainty):
                self.error = None

    async def _stop_collector(self):
        """握手失败在连接和文件已关闭后作为运行错误返回，不误标为隔离失败。"""
        await stop_session_monitors(self)
        try:
            await self.collector.stop()
        except PshSwitchError as exc:
            self.error = str(exc)

    def background_failure(self):
        """读取终结异常用于停止结果，不把正常取消重新抛给监督器。"""
        if not self.background.done() or self.background.cancelled():
            return None
        return self.background.exception()

    def tail(self, cursor):
        """按运行游标读取有界缓冲；超出保存范围返回 gap 供客户端显式补读。"""
        number = 0
        if cursor:
            try:
                run, value = cursor.rsplit(":", 1)
                number = int(value) if run == self.task["runId"] else 0
            except (ValueError, AttributeError):
                number = 0
        frames = [frame for frame in self.frames if int(frame["cursor"].rsplit(":", 1)[1]) > number]
        if not cursor:
            # 首次订阅只展示最新四帧，避免打开页面回放整个运行缓冲；重连携带 cursor 时返回全部连续帧。
            frames = frames[-4:]
        # 缓冲本身已按 8 MiB 限制；一次返回所有连续帧，避免 100ms 轮询的固定帧数成为高频日志瓶颈。
        return {"frames": frames, "gap": bool(frames and cursor and int(frames[0]["cursor"].rsplit(":", 1)[1]) > number+1)}

    async def wait_tail(self, cursor, wait_seconds: float = 0) -> dict:
        """先读取再有界等待下一帧；停止或替换运行时立即结束旧实例的等待。"""
        if wait_seconds <= 0:
            return self.tail(cursor)
        # 读取、记录序号和条件等待必须在同一把锁下：on_log 不能在空读与
        # wait_for 之间插入通知，避免客户端漏掉刚到达的一帧后白等一个周期。
        async with self.tail_changed:
            before = self.frame_number
            result = self.tail(cursor)
            if result["frames"] or result["gap"] or self.stopping or self.retired:
                return result
            try:
                await asyncio.wait_for(
                    self.tail_changed.wait_for(
                        lambda: self.frame_number != before or self.stopping or self.retired,
                    ),
                    timeout=wait_seconds,
                )
            except TimeoutError:
                pass
            return self.tail(cursor)

    async def _wake_tail_waiters(self) -> None:
        """停止或隔离旧运行时唤醒其内部 tail，避免等待者占满整个最长窗口。"""
        async with self.tail_changed:
            self.tail_changed.notify_all()
