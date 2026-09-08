"""PSH 调试握手：旁路观察唯一接收流，全部写操作由采集器发送队列独占。

这里只解析协议响应，不主动读取连接，不解码二维码，也不把密文或口令加入事件。
观察窗口有界，原始设备输出仍由原采集链路完整保存。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import inspect
import re
from collections.abc import Callable, Mapping
from typing import Any

from .psh_response import PshResponse

_ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_MODE = re.compile(rb"(?:Protect Shell|built-in shell)\s*\((psh|ash)\)", re.IGNORECASE)
_ASH_COMMAND_ERROR = re.compile(rb"(?:^|[\r\n])(?:/bin/)?-?(?:a?sh):\s*debug:\s*(?:not found|command not found)", re.IGNORECASE)
_SOURCE = re.compile(rb"^[ \t]*(?:enc_string:[ \t]*)?([A-Za-z0-9+/]{32,}={0,2})[ \t]*\r?$", re.MULTILINE)
_PASSWORD = re.compile(rb"password\s*:", re.IGNORECASE)
_PROMPT = re.compile(rb"(?:^|[\r\n])[^\r\n]{0,80}[#$][ \t]*(?:$|[\r\n])")
_LS_UNSUPPORTED = re.compile(rb"['\"]?ls['\"]?\s+Not Supported,\s*Try\s*['\"]?help", re.IGNORECASE)
_LS_ECHO = re.compile(rb"(?:^|[\r\n])(?:[^\r\n]{0,80}[#$][ \t]*)?ls\r?(?:\n|$)")


class PshSwitchError(ConnectionError):
    """模式切换未被确认，必须终止会话，避免后续命令被设备当作口令。"""


def extract_challenge(data: bytes) -> str | None:
    """提取 Password 提示前最后一条完整 Base64 密文，保持原字符串不解码提交。

    换行与 Password 提示共同限定完整响应，避免网络半包被误作完整密文。
    Base64 解码仅用于格式校验；二维码块、命令回显及普通日志不符合候选规则。
    """
    clean = _ANSI.sub(b"", data)
    prompts = list(_PASSWORD.finditer(clean))
    if not prompts:
        return None
    start = prompts[-2].end() if len(prompts) > 1 else 0
    preceding = clean[start:prompts[-1].start()]
    for line in preceding.splitlines():
        # 密文样式前缀后出现无法识别的混合文本时，不把下一行残片当成完整密文。
        if re.match(rb"^[ \t]*(?:enc_string:[ \t]*)?[A-Za-z0-9+/=]{32,}", line) and not _SOURCE.fullmatch(line):
            return None
    for match in reversed(list(_SOURCE.finditer(preceding))):
        source = match.group(1)
        if len(source) > 16384:
            continue
        try:
            base64.b64decode(source, validate=True)
        except (ValueError, binascii.Error):
            continue
        return source.decode("ascii")
    return None


async def _notify(callback, event, details):
    """兼容同步测试回调与异步运行事件存储，参数不含设备密文或口令。"""
    if callback:
        result = callback(event, details)
        if inspect.isawaitable(result):
            await result


class PshDialogue:
    """每个采集会话独立持有模式和握手窗口，不在任务或模板间共享。"""

    def __init__(self, task: Mapping[str, Any], provider: Callable | None, on_event=None):
        self.task, self.provider, self.on_event = task, provider, on_event
        self.mode = "UNKNOWN"
        self._mode_tail = b""
        self._buffer = b""
        self._response = PshResponse()
        self._source = None
        self._active = False
        self._closed = False
        self._changed = asyncio.Event()

    def feed(self, data: bytes) -> None:
        """由唯一接收协程调用；只观察，不消费、不修改传给日志写入器的数据。"""
        combined = self._mode_tail + data
        # 普通高吞吐日志绝大多数不含模式标记，先用字节查找避免逐块全量正则扫描。
        if any(marker in combined for marker in (b"(ash)", b"(psh)", b"(ASH)", b"(PSH)")):
            matches = list(_MODE.finditer(_ANSI.sub(b"", combined)))
            if matches:
                self.mode = matches[-1].group(1).decode().upper()
        self._mode_tail = combined[-128:]
        if self._active:
            self._response.feed(data)
            self._buffer = self._response.data
            # 一旦取得完整密文行即独立保留，普通日志不能挤掉尚待 Password 提示的密文。
            complete = self._buffer.rsplit(b"\n", 1)[0] if b"\n" in self._buffer else b""
            candidate = extract_challenge(complete + b"\nPassword:")
            if candidate:
                self._source = candidate
        self._changed.set()

    def close(self) -> None:
        """连接关闭立即唤醒握手等待，不让口令写入已经结束或新建的会话。"""
        self._closed = True
        self._changed.set()

    async def _wait_response(self, *, confirming: bool = False):
        """等待新响应中的模式、密文或不支持提示；未知提示不能视为成功。"""
        while True:
            self._changed.clear()
            if self._closed:
                raise PshSwitchError("PSH 切换期间连接关闭")
            clean = _ANSI.sub(b"", self._buffer)
            matches = list(_MODE.finditer(clean))
            if matches and matches[-1].group(1).lower() == b"ash":
                self.mode = "ASH"
                return "ASH", None
            if not confirming and _ASH_COMMAND_ERROR.search(clean):
                self.mode = "ASH"
                return "ASH", None
            if confirming:
                if matches or re.search(rb"(?:incorrect|invalid|wrong)\s+password|password\s+(?:incorrect|invalid|wrong)|authentication\s+failed", clean, re.IGNORECASE) or _PASSWORD.search(clean):
                    raise PshSwitchError("设备未确认 ASH 模式，口令可能已失效")
                if _PROMPT.search(clean):
                    return "PROMPT", None
            else:
                source = extract_challenge(clean) or (self._source if _PASSWORD.search(clean) else None)
                if source:
                    self.mode = "PSH"
                    return "CHALLENGE", source
                # 普通登录横幅也包含 help，不能把它误当作 debug 不受支持的响应。
                if re.search(rb"(?:unknown command|not found|unsupported command|supported commands:)", clean, re.IGNORECASE) and re.search(rb"(?:^|[\r\n])#\s*(?:$|[\r\n])", clean):
                    return "FALLBACK", None
            await self._changed.wait()

    async def _probe_ls(self, write, newline):
        """无可靠横幅时用 ls 探测；必须观察到本次回显/目录及返回提示符才认定 ASH。"""
        self._buffer, self._response, self._source = b"", PshResponse(), None
        await write(("ls" + newline).encode())
        while True:
            self._changed.clear()
            if self._closed:
                raise PshSwitchError("ls 模式探测期间连接关闭")
            clean = _ANSI.sub(b"", self._buffer)
            prompts = list(_PROMPT.finditer(clean))
            prompt_end = prompts[-1].end() if prompts else -1
            unsupported = _LS_UNSUPPORTED.search(clean)
            if unsupported and prompt_end >= unsupported.end():
                self.mode = "PSH"
                return
            failure = re.search(rb"(?:not supported|not found|permission denied|cannot access)", clean, re.IGNORECASE)
            # 不支持提示可能分包，必须等到返回提示符后再区分 PSH 特征与普通错误。
            if not unsupported and (_PASSWORD.search(clean) or (failure and prompt_end >= failure.end())):
                raise PshSwitchError("ls 未正常完成，无法确认设备模式")
            echoes = list(_LS_ECHO.finditer(clean))
            listing = re.search(rb"\b(?:davinci|bin|sbin|etc|proc|usr)\b", clean)
            complete = (echoes and prompt_end >= echoes[-1].end()) or (listing and prompt_end >= listing.end())
            if not unsupported and complete:
                self.mode = "ASH"
                return
            await self._changed.wait()

    async def observe_initial_mode(self) -> None:
        """初始化前短暂等候横幅，由唯一 reader 读取，不占用定时发送预算。"""
        if self.mode == "UNKNOWN":
            try:
                async with asyncio.timeout(.25):
                    while self.mode == "UNKNOWN" and not self._closed:
                        self._changed.clear()
                        await self._changed.wait()
            except TimeoutError:
                pass

    async def ensure_ash(self, write: Callable, newline: str, timeout: float) -> None:
        """串行完成 debug、解密、口令发送及 ASH 确认；失败不自动再次尝试口令。"""
        if self.mode == "ASH":
            await _notify(self.on_event, "ALREADY_ASH", {"mode": "ASH"})
            return
        self._active, self._buffer = True, b""
        self._response, self._source = PshResponse(), None
        awaiting_challenge = False
        try:
            async with asyncio.timeout(timeout):
                await _notify(self.on_event, "STARTED", {"mode": self.mode})
                if self.mode == "UNKNOWN":
                    await self._probe_ls(write, newline)
                    if self.mode == "ASH":
                        await _notify(self.on_event, "ALREADY_ASH", {"mode": "ASH"})
                        return
                    self._buffer, self._response, self._source = b"", PshResponse(), None
                awaiting_challenge = True
                await write(("debug" + newline).encode())
                response, source = await self._wait_response()
                if response == "FALLBACK":
                    self._buffer = b""
                    self._response, self._source = PshResponse(), None
                    await write(("zhimakaimen" + newline).encode())
                    response, source = await self._wait_response()
                awaiting_challenge = False
                if response == "ASH":
                    await _notify(self.on_event, "ALREADY_ASH", {"mode": "ASH"})
                    return
                if response != "CHALLENGE" or not self.provider:
                    raise PshSwitchError("未配置可用的 PSH 解密服务")
                await _notify(self.on_event, "CHALLENGE_RECEIVED", {"mode": "PSH"})
                result = self.provider(self.task, source)
                password = await result if inspect.isawaitable(result) else result
                if not isinstance(password, str) or not password or len(password) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in password):
                    raise PshSwitchError("PSH 解密服务返回无效口令")
                self._buffer = b""
                self._response, self._source = PshResponse(), None
                await write((password + newline).encode())
                response, _ = await self._wait_response(confirming=True)
                if response == "PROMPT":
                    await self._probe_ls(write, newline)
                    if self.mode != "ASH":
                        raise PshSwitchError("口令提交后 ls 仍处于 PSH 模式")
                await _notify(self.on_event, "ASH_READY", {"mode": "ASH"})
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 第三方错误统一转换为不含密文口令的领域异常。
            # 隔离第三方异常文本，防止请求参数、响应正文或解密口令进入日志。
            await _notify(self.on_event, "FAILED", {"mode": self.mode})
            if awaiting_challenge:
                # 锁定设备可能只返回普通打印；没有完整密文不能调用解密或试探性发送口令。
                raise PshSwitchError("未收到完整调试密文，疑似锁定或响应超时；已停止任务，不自动重试") from None
            raise PshSwitchError("PSH 到 ASH 切换失败或超时，已终止本次会话") from None
        finally:
            self._active, self._buffer = False, b""
            self._response, self._source = PshResponse(), None
