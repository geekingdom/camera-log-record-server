"""从机登录握手与接收交接：整个连接只有一个底层读取者，主机握手不冒充从机日志。"""

import asyncio
import re
import uuid
from collections import deque

import asyncssh

from camera_logs.collection.psh_dialogue import PshDialogue

SLAVE_ADDRESSES = {"SLAVE_1": "192.168.253.164", "SLAVE_2": "192.168.253.166", "SLAVE_3": "192.168.253.167"}
_SHELL = re.compile(rb"(?:built-in shell|Protect Shell)\s*\((?:ash|psh)\)", re.IGNORECASE)
_DENIED = re.compile(rb"permission denied|authentication failed|password:\s*$", re.IGNORECASE)


class SlaveLoginError(ConnectionError):
    """从机握手未确认；不得进入初始化或把外层主机输出保存为从机日志。"""


class ShellBootstrap:
    """引导阶段旁路观察响应，成功后以有界队列交给原采集器，关闭仍释放实际SSH名额。"""

    def __init__(self, connection, task, provider=None, guard=None):
        self.connection = connection
        self.guard = guard
        self.dialogue = PshDialogue(task, provider)
        self.buffer = b""
        self.changed = asyncio.Event()
        self.queue = deque()
        self.data_available = asyncio.Event()
        self.space_available = asyncio.Event()
        self.space_available.set()
        self.ready = False
        self.eof = False
        self.pending = b""
        self.reader = asyncio.create_task(self._receive())

    async def _receive(self):
        """握手窗口有界，采集阶段背压传递到底层SSH，不启动第二个socket接收器。"""
        try:
            while True:
                if self.ready and len(self.queue) >= 16:
                    self.space_available.clear()
                    await self.space_available.wait()
                data = await self.connection.read(65536)
                if not data:
                    break
                if self.ready:
                    self.queue.append(data)
                    self.data_available.set()
                else:
                    self.dialogue.feed(data)
                    self.buffer = (self.buffer + data)[-131072:]
                    self.changed.set()
        finally:
            self.eof = True
            self.dialogue.close()
            self.changed.set()
            self.data_available.set()

    async def wait_match(self, pattern, timeout=20, *, authenticating=False):
        """只匹配当前阶段的新响应；连接关闭或超时一律失败，错误不包含密码与设备正文。"""
        async with asyncio.timeout(timeout):
            while True:
                self.changed.clear()
                found = pattern.search(self.buffer)
                if found:
                    return found
                if authenticating and _DENIED.search(self.buffer):
                    raise asyncssh.PermissionDenied("从机SSH认证失败，请检查任务的设备登录密码")
                if self.eof:
                    raise SlaveLoginError("从机登录期间连接关闭")
                await self.changed.wait()

    async def ensure_ash(self):
        """先识别主机模式，PSH才调用已有解密器；默认ASH不发送debug。"""
        await self.dialogue.observe_initial_mode()
        await self.dialogue.ensure_ash(self._guarded_write, "\n", 20)

    async def enter_slave(self, target, password):
        """等待指定内网地址密码提示，单次提交密码，新的ASH/PSH横幅均代表进入从机。"""
        address = SLAVE_ADDRESSES[target]
        if any(c in password for c in ("\r", "\n", "\x00")):
            raise SlaveLoginError("从机交互登录密码不支持换行或空字符")
        await self.ensure_ash()
        self.buffer = b""
        # dbclient无论正常退出还是失败均关闭父shell，避免后续主机打印混入从机日志。
        await self._guarded_write(f"dbclient admin@{address} -y; exit\n".encode())
        await self.wait_match(re.compile(re.escape(f"admin@{address}".encode()) + rb"[^\r\n]*password:\s*", re.IGNORECASE))
        self.buffer = b""
        await self._guarded_write((password + "\n").encode())
        found = await self.wait_match(_SHELL, authenticating=True)
        # 同一事件循环内无await完成切换；此前密码提示和主机横幅不会进入日志。
        self.pending = self.buffer[found.start():]
        self.buffer = b""
        self.ready = True
        return self

    async def command(self, command):
        """引导专用连接执行固定管理命令，以随机完成标记确认shell返回。"""
        marker = "__SLAVE_BOOT_" + uuid.uuid4().hex
        self.buffer = b""
        await self._guarded_write(f'{command}; printf "\\n{marker}:%s\\n" "$?"\n'.encode())
        matched = await self.wait_match(re.compile(rb"\r?\n" + marker.encode() + rb":([0-9]+)\r?\n"))
        return int(matched.group(1))

    async def _guarded_write(self, data):
        """所有引导字节发送前复核任务状态，取消或资格失效不会继续提交口令/管理命令。"""
        if self.guard:
            await self.guard()
        if self.eof:
            raise SlaveLoginError("引导连接已经关闭")
        await self.connection.write(data)

    async def read(self, size=65536):
        """采集器消费单读取者已接收的数据；EOF之前先排空所有已接收块。"""
        while not self.pending:
            self.data_available.clear()
            if self.queue:
                self.pending = self.queue.popleft()
                self.space_available.set()
            elif self.eof:
                if not self.reader.cancelled() and self.reader.exception():
                    raise SlaveLoginError("从机日志连接读取失败") from None
                return b""
            else:
                # 取消等待只取消Event，不会先从队列取走数据后丢失在超时边界。
                await self.data_available.wait()
        result, self.pending = self.pending[:size], self.pending[size:]
        return result

    @property
    def has_buffered_data(self):
        """正常停止仍需排空已接收字节；EOF之前继续等待关闭动作完成。"""
        return bool(self.pending or self.queue or not self.eof)

    async def write(self, data):
        """后续初始化、定时和手动命令仍复用同一条从机shell输入。"""
        await self.connection.write(data)

    async def close(self):
        """先结束读取者，再确认外层SSH关闭并释放名额；失败继续交给原隔离流程。"""
        self.reader.cancel()
        await asyncio.gather(self.reader, return_exceptions=True)
        await self.connection.close()
