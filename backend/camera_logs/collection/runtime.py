"""把设备会话接入任务运行、日志目录和持久化命令预算。

同一次运行可以包含多个重连会话，预算按运行 ID 和命令 ID 累计；实时缓冲有界，
原始日志由存储层完整保存。异常重连创建新会话，不能复用旧会话手动命令。
"""
import asyncio
import base64
import hashlib
import logging
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pymongo import ReturnDocument

from camera_logs.collection.collector import Collector
from camera_logs.common.database import now
from camera_logs.common.models import new_id

logger = logging.getLogger(__name__)


class SessionRuntime:
    """节点独占的单任务运行，负责状态回写、重连和持久化执行记录。"""
    def __init__(self, repo, task, connection_factory):
        self.repo, self.task, self.factory = repo, task, connection_factory
        self.collector = None
        self.stopping = False
        self.frames = deque()
        self.frame_bytes = 0
        self.frame_number = 0
        self.paths = {}
        self.last_catalog = 0.
        self.pending_executions = {}
        self.input_bytes = 0
        self.error = None
        self.started_at = now()
        self.background = asyncio.create_task(self.run())

    def file_id(self, path):
        """原始文件与压缩文件共享逻辑 ID，路径包含运行及会话以避免跨任务碰撞。"""
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
            self.last_catalog = time.monotonic()
            hour = self._hour_from_path(path)
            await self.repo.db.files.update_one({"id": file_id}, {"$set": {
                "id": file_id, "taskId": chunk.task_id, "runId": chunk.run_id, "sessionId": chunk.session_id,
                "nodeId": self.repo.settings.node_id, "hour": hour, "path": str(path), "bytes": self.paths[file_id],
                "status": "OPEN", "runStartedAt": self.task.get("runStartedAt", self.started_at),
                "rawFileName": path.name,
                "sessionStartedAt": self.started_at, "updatedAt": now()},
                "$setOnInsert": {"createdAt": now(), "firstSequence": chunk.sequence}}, upsert=True)

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
        file_id = self.file_id(path)
        await self.repo.db.files.update_one({"id": file_id}, {"$set": {
            "id": file_id, "taskId": archive.task_id, "runId": archive.run_id, "sessionId": archive.session_id,
            "nodeId": self.repo.settings.node_id,
            "hour": archive.hour_start.astimezone(UTC).isoformat(),
            "path": str(path),
            "archiveName": path.name,
            "bytes": archive.raw_size, "archiveBytes": path.stat().st_size, "sha256": archive.sha256,
            "firstSequence": archive.first_sequence, "lastSequence": archive.last_sequence, "status": "READY",
            "runStartedAt": self.task.get("runStartedAt", self.started_at), "sessionStartedAt": self.started_at,
            "updatedAt": now()}}, upsert=True)

    async def on_state(self, state, details):
        """将会话状态映射到运行状态；压缩失败单独告警，不伪装成连接中断。"""
        if state == "CLOSED":
            state = "STOPPING" if self.stopping else "RECONNECTING"
        elif state in {"READ_ERROR", "IDLE_TIMEOUT"}:
            # 网络读失败和无日志超时会由本运行实例重连，前端统一展示重连中。
            state = "RECONNECTING"
        if state == "ARCHIVE_ERROR":
            await self.repo.db.tasks.update_one(
                {"id": self.task["id"], "runId": self.task["runId"]},
                {"$set": {"archiveError": details.get("error"), "updatedAt": now()}},
            )
            return
        await self.repo.db.tasks.update_one({"id": self.task["id"], "runId": self.task["runId"]},
            {"$set": {"status": state, "sessionId": details.get("sessionId"), "updatedAt": now()}})
        if state == "COLLECTING":
            await self.repo.db.operations.update_many({"taskId": self.task["id"], "desiredState": "RUNNING", "status": "PENDING"},
                {"$set": {"status": "SUCCEEDED", "completedAt": now()}})
        logger.info("采集状态变化", extra={"context": {"taskId": self.task["id"], "state": state}})

    async def reserve(self, command_id, details):
        """发送前原子占用一次预算；不确定结果不退回次数，防止重连后重复发送。"""
        command = next(c for c in self.task["scheduledCommands"] if c["id"] == command_id)
        budget_id = f'{self.task["runId"]}:{command_id}'
        await self.repo.db.budgets.update_one({"_id": budget_id}, {"$setOnInsert": {"attempts": 0}}, upsert=True)
        budget = await self.repo.db.budgets.find_one_and_update({"_id": budget_id, "attempts": {"$lt": command["totalExecutions"]}},
            {"$inc": {"attempts": 1}}, return_document=ReturnDocument.AFTER)
        if not budget:
            return False
        execution = {"id": new_id(), "taskId": self.task["id"], "runId": self.task["runId"],
            "sessionId": self.collector.session_id, "commandId": command_id, "kind": "SCHEDULED",
            "attempt": budget["attempts"], "status": "SENDING", "createdAt": now()}
        await self.repo.db.commands.insert_one(execution)
        self.pending_executions[command_id] = execution["id"]
        return True

    async def update_execution(self, command_id, status, details):
        """按本会话的执行 ID 更新结果，模板与其他任务不共享进度。"""
        identifier = self.pending_executions.pop(command_id, None)
        if identifier:
            await self.repo.db.commands.update_one({"id": identifier}, {"$set": {"status": status, "completedAt": now()}})

    async def manual(self, document):
        """只在命令绑定的会话发送；已过期会话取消，进入发送后异常记为未知。"""
        claimed = await self.repo.db.commands.find_one_and_update({"id": document["id"], "status": "QUEUED"},
            {"$set": {"status": "SENDING", "startedAt": now()}}, return_document=ReturnDocument.AFTER)
        if not claimed:
            return
        status = "UNKNOWN"
        try:
            if self.stopping or not self.collector or document.get("sessionId") != self.collector.session_id:
                status = "CANCELLED"
            else:
                # collector 内部负责提示符等待及发送后延时，外层不能提前取消其串行收尾。
                await self.collector.enqueue_manual(
                    document["command"],
                    newline=document.get("newline", "\n"),
                    prompt=document.get("prompt"),
                    timeout_seconds=float(document.get("timeoutSeconds", 30)),
                    delay_seconds=float(document.get("delaySeconds", 0)),
                )
                status = "SENT"
        except Exception:
            logger.exception("手动命令结果未知 task=%s command=%s", self.task["id"], document["id"])
        finally:
            await self.repo.db.commands.update_one({"id": document["id"]},
                {"$set": {"status": status, "completedAt": now()}})

    async def run(self):
        """连接失败指数退避，先释放旧连接再重试；每次登录后重新发送初始化命令。"""
        delay = 1
        while not self.stopping:
            try:
                config = dict(self.task)
                config["password"] = self.repo.decrypt(config["passwordEncrypted"])
                config["knownHosts"] = self.repo.settings.known_hosts
                self.started_at = now()
                self.collector = Collector(config, self.repo.settings.log_root, connection_factory=self.factory,
                    on_log=self.on_log, on_state=self.on_state, on_archive=self.on_archive,
                    reserve_execution=self.reserve, update_execution=self.update_execution)
                await self.collector.start()
                delay = 1
                await self.collector.wait_closed()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("采集会话异常 task=%s", self.task["id"])
                import asyncssh
                if isinstance(exc, (asyncssh.PermissionDenied, asyncssh.HostKeyNotVerifiable)):
                    self.error = "SSH认证或主机密钥校验失败"
                    break
            finally:
                if self.collector:
                    await self.collector.stop()
                await self.repo.db.commands.update_many({"taskId": self.task["id"], "status": "QUEUED"},
                    {"$set": {"status": "CANCELLED", "completedAt": now()}})
                await self.repo.db.commands.update_many({"taskId": self.task["id"], "status": "SENDING"},
                    {"$set": {"status": "UNKNOWN", "completedAt": now()}})
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
        if self.collector:
            await self.collector.stop()
        if not self.background.done():
            self.background.cancel()
        await asyncio.gather(self.background, return_exceptions=True)

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
