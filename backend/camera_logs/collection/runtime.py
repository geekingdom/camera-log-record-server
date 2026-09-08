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

from pymongo import ReturnDocument

from camera_logs.collection.collector import Collector, CommandChannelBlocked
from camera_logs.collection.psh_dialogue import PshSwitchError
from camera_logs.collection.psh_passwords import PshPasswordProvider
from camera_logs.commands.reservation import ReservationUncertain, reserve_scheduled
from camera_logs.common.database import now
from camera_logs.common.ownership import OwnershipLost, owner_filter

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
        self.paths = {}
        self.catalog_dirty = {}
        self.catalog_ready = set()
        self.catalog_lock = asyncio.Lock()
        self.last_catalog = 0.
        self.pending_executions = {}
        self.input_bytes = 0
        self.error = None
        self.started_at = now()
        self.debug_passwords = PshPasswordProvider(repo.settings)
        # 目录水位不能只依赖下一块日志触发；设备停止输出后的尾块同样需要发布。
        self.catalog_task = asyncio.create_task(self._catalog_loop())
        self.background = asyncio.create_task(self.run())

    async def on_debug(self, event, details):
        """记录调试模式切换阶段，只保存任务身份与模式，不保存密文或解密口令。"""
        command_blocked = bool(details.get("commandBlocked", False))
        debug_error = details.get("debugError")
        await self.repo.db.events.insert_one({"taskId": self.task["id"], "runId": self.task["runId"],
            "sessionId": self.collector.session_id, "type": "DEBUG_MODE", "phase": event,
            "mode": details["mode"], "commandBlocked": command_blocked,
            "debugError": debug_error, "createdAt": now()})
        if not getattr(self, "retired", False):
            await self.repo.db.tasks.update_one(owner_filter(self.task),
                {"$set": {"shellMode": details["mode"], "debugPhase": event,
                    "commandBlocked": command_blocked, "debugError": debug_error, "updatedAt": now()}})
        logger.info("设备调试模式交互 task=%s phase=%s mode=%s", self.task["id"], event, details["mode"])

    def file_id(self, path):
        """以不可变原始分卷路径生成逻辑 ID，多个成员可共享一个小时归档。"""
        relative = str(Path(path).relative_to(self.repo.settings.log_root))
        return hashlib.sha256(relative.removesuffix(".tar.gz").removesuffix(".log").encode()).hexdigest()[:32]

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
        frame = {"type": "data", "fileId": file_id, "sessionId": chunk.session_id,
                 "runId": chunk.run_id, "offset": chunk.offset, "endOffset": end_offset,
                 "cursor": f'{self.task["runId"]}:{self.frame_number}',
                 "data": base64.b64encode(chunk.data).decode(), "size": len(chunk.data)}
        self.frames.append(frame)
        self.frame_bytes += len(chunk.data)
        while self.frame_bytes > 8*1024*1024 and self.frames:
            self.frame_bytes -= self.frames.popleft()["size"]
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
                await self.repo.db.files.update_one({"id": file_id}, {"$set": value,
                    "$setOnInsert": set_on_insert}, upsert=True)
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
            year, month, day, hour = (int(value) for value in path.parts[-5:-1])
            local = datetime(year, month, day, hour, tzinfo=ZoneInfo("Asia/Shanghai"))
            return local.astimezone(UTC).isoformat()
        except (TypeError, ValueError):
            return now().astimezone(UTC).isoformat()

    async def on_archive(self, archive):
        """仅在压缩校验和原子发布成功后把目录记录设为可下载。"""
        path = archive.path
        log_path = getattr(archive, "log_path", None) or path
        file_id = self.file_id(log_path)
        member_name = getattr(archive, "member_name", None)
        index_path = getattr(archive, "index_path", None)
        member_fields = {}
        if member_name:
            member_fields = {"archiveMember": member_name, "rawFileName": member_name,
                "archiveGroupId": hashlib.sha256(str(path.relative_to(self.repo.settings.log_root)).encode()).hexdigest(),
                "segmentNumber": self._segment_number(log_path)}
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
                "archiveName": path.name, "bytes": archive.raw_size, "archiveBytes": path.stat().st_size,
                "sha256": archive.sha256, "firstSequence": archive.first_sequence,
                "lastSequence": archive.last_sequence, "status": "READY", **member_fields,
                "runStartedAt": self.task.get("runStartedAt", self.started_at),
                "sessionStartedAt": self.started_at}
        # READY 也由统一发布器落库。失败时条目保留，下一周期不会遗漏已删原始正文。
        await self._publish_catalog()

    async def on_state(self, state, details):
        """将会话状态映射到运行状态；压缩失败单独告警，不伪装成连接中断。"""
        if state == "CLOCK_ROLLBACK":
            # 时钟异常属于可追踪事件，不覆盖采集中状态；文件身份沿用该块的不可变路径。
            await self.repo.db.events.insert_one({
                "type": state, "taskId": self.task["id"], "runId": self.task["runId"],
                "sessionId": details["sessionId"], "nodeId": self.repo.settings.node_id,
                "previousReceivedAt": details["previousReceivedAt"], "receivedAt": details["receivedAt"],
                "sequence": details["sequence"], "fileId": self.file_id(details["path"]),
                "message": "服务器接收时间回拨，日志已另起片段，按块序号保持接收顺序", "createdAt": now(),
            })
            logger.warning("采集接收时间回拨", extra={"context": {
                "taskId": self.task["id"], "sessionId": details["sessionId"],
                "previousReceivedAt": details["previousReceivedAt"], "receivedAt": details["receivedAt"],
            }})
            return
        if getattr(self, "retired", False):
            if state == "CONNECTING":
                raise OwnershipLost("运行实例已隔离，禁止重新连接")
            return
        if state == "CLOSED":
            state = "STOPPING" if self.stopping else "RECONNECTING"
        elif state in {"READ_ERROR", "IDLE_TIMEOUT"}:
            # 网络读失败和无日志超时会由本运行实例重连，前端统一展示重连中。
            state = "RECONNECTING"
        if state == "ARCHIVE_ERROR":
            await self.repo.db.tasks.update_one(
                owner_filter(self.task),
                {"$set": {"archiveError": details.get("error"), "updatedAt": now()}},
            )
            return
        changed = await self.repo.db.tasks.update_one({**owner_filter(self.task), "status": {"$ne": "BLOCKED"}},
            {"$set": {"status": state, "sessionId": details.get("sessionId"), "updatedAt": now()}})
        if state == "CONNECTING" and not changed.matched_count:
            raise OwnershipLost("建连前任务归属或准入已失效")
        if state == "COLLECTING" and changed.matched_count:
            await self.repo.db.operations.update_many({"taskId": self.task["id"], "desiredState": "RUNNING", "status": "PENDING"},
                {"$set": {"status": "SUCCEEDED", "completedAt": now()}})
        logger.info("采集状态变化", extra={"context": {"taskId": self.task["id"], "state": state}})

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
            await self.repo.db.commands.update_one({"id": identifier, "taskId": self.task["id"],
                "runId": self.task["runId"], "sessionId": session_id, "commandId": command_id,
                "kind": "SCHEDULED", "status": "SENDING"},
                {"$set": {"status": status, "completedAt": now()}})
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
        current = await self.repo.db.tasks.find_one({**owner_filter(self.task), "sessionId": collector.session_id,
                                                   "status": "COLLECTING", "desiredState": "RUNNING"})
        if current is None:
            return
        scope = {"id": record["id"], "taskId": self.task["id"], "kind": "MANUAL",
                 "runId": record.get("runId"), "sessionId": record.get("sessionId")}
        if record.get("runId") != self.task["runId"] or record.get("sessionId") != collector.session_id:
            await self.repo.db.commands.update_one({**scope, "status": "QUEUED"},
                {"$set": {"status": "CANCELLED", "completedAt": now()}})
            return
        claimed = await self.repo.db.commands.find_one_and_update({**scope, "status": "QUEUED"},
            {"$set": {"status": "SENDING", "startedAt": now()}}, return_document=ReturnDocument.AFTER)
        if not claimed:
            return
        status = "UNKNOWN"
        error = None
        try:
            if self.stopping or getattr(self, "retired", False) or self.collector is not collector:
                status = "CANCELLED"
            else:
                # collector 内部负责提示符等待及发送后延时，外层不能提前取消其串行收尾。
                await collector.enqueue_manual(
                    claimed["command"],
                    newline=claimed.get("newline", "\n"),
                    prompt=claimed.get("prompt"),
                    timeout_seconds=float(claimed.get("timeoutSeconds", 30)),
                    delay_seconds=float(claimed.get("delaySeconds", 0)),
                )
                status = "SENT"
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

    async def run(self):
        """连接失败指数退避，先释放旧连接再重试；每次登录后重新发送初始化命令。"""
        delay = 1
        while not self.stopping:
            session_commands = None
            try:
                config = dict(self.task)
                config["password"] = self.repo.decrypt(config["passwordEncrypted"])
                config["knownHosts"] = self.repo.settings.known_hosts
                config["verifyHostKey"] = self.repo.settings.ssh_verify_host_key
                config["pshSerialCharacterInterval"] = self.repo.settings.psh_serial_character_interval
                self.started_at = now()
                reset = {"shellMode": "UNKNOWN", "debugPhase": None, "commandBlocked": False, "debugError": None}
                admitted = await self.repo.db.tasks.update_one(
                    {**owner_filter(self.task), "status": {"$ne": "BLOCKED"}},
                    {"$set": reset})
                if not admitted.matched_count:
                    raise OwnershipLost("重连前任务归属或准入已失效")
                self.collector = Collector(config, self.repo.settings.log_root, connection_factory=self.factory,
                    on_log=self.on_log, on_state=self.on_state, on_archive=self.on_archive,
                    reserve_execution=self.reserve, update_execution=self.update_execution,
                    resolve_debug_password=self.debug_passwords, on_debug=self.on_debug)
                # 收尾条件绑定本次实际创建的会话，旧运行不能取消后继会话的命令。
                session_commands = {"taskId": self.task["id"], "runId": self.task["runId"],
                                    "sessionId": self.collector.session_id}
                await self.collector.start()
                delay = 1
                await self.collector.wait_closed()
            except asyncio.CancelledError:
                raise
            except OwnershipLost:
                self.retired = self.stopping = True
                logger.info("运行实例停止重连，归属或准入已失效 task=%s", self.task["id"])
                break
            except PshSwitchError as exc:
                self.error = str(exc)
                break
            except Exception as exc:
                logger.exception("采集会话异常 task=%s", self.task["id"])
                import asyncssh
                if isinstance(exc, asyncssh.PermissionDenied):
                    # 凭据被拒绝不会因重试自行恢复，保留失败状态等待管理员修正账号配置。
                    self.error = "SSH账号认证失败"
                    break
                if isinstance(exc, asyncssh.HostKeyNotVerifiable):
                    # 指纹缺失或变化必须人工确认，绝不能绕过 known_hosts 自动重连。
                    self.error = "SSH主机指纹未登记或不匹配"
                    break
            finally:
                if self.collector:
                    await self._stop_collector()
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
                await self.repo.db.events.insert_one({"taskId": self.task["id"], "runId": self.task["runId"],
                    "type": "CONNECTION_GAP", "detectedAt": now(), "message": "连接中断，设备端未提供补传"})
                await self.on_state("RECONNECTING", {"sessionId": self.collector.session_id})
                import random
                await asyncio.sleep(delay+random.random())
                delay = min(delay*2, 60)

    async def stop(self):
        """禁用重试并等待采集器收尾，连接释放完成后才能取消运行等待。"""
        self.stopping = True
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

    async def _stop_collector(self):
        """握手失败在连接和文件已关闭后作为运行错误返回，不误标为隔离失败。"""
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
            frames = frames[-4:]
        return {"frames": frames[:32], "gap": bool(frames and cursor and int(frames[0]["cursor"].rsplit(":", 1)[1]) > number+1)}
