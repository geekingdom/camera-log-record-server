"""从串口混合输出中旁路识别交互响应，保持日志主链路原始字节不变。

设备 syslog 可以插入密文、Password 提示或 ASH 横幅中间。这里仅剔除格式明确的
syslog 记录，并拼回它前后的交互片段；无法识别的内容保留，由上层严格校验。
"""

from __future__ import annotations

import re

_ANSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
_SYSLOG = re.compile(
    rb"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+"
    rb"\d{2}:\d{2}:\d{2}\s+[a-zA-Z0-9_-]+\."
    rb"(?:emerg|alert|crit|err|error|warn|warning|notice|info|debug)\s+"
)


class PshResponse:
    """有界增量响应窗口；过滤规则只作用于握手识别，不得用于日志正文写入。"""

    def __init__(self):
        self._pending = b""
        self._control = b""
        self._dropping_log = False

    def feed(self, data: bytes) -> None:
        """跨 TCP 分包等待完整行；已确认的 syslog 尾部可立即丢出观察窗口。"""
        parts = (self._pending + data).split(b"\n")
        self._pending = parts.pop()
        for part in parts:
            if self._dropping_log:
                self._dropping_log = False
                continue
            clean = _ANSI.sub(b"", part)
            log = _SYSLOG.search(clean)
            if log:
                # syslog 的换行属于插入记录，不能用它截断之前的半个密文或提示符。
                self._control = (self._control + clean[:log.start()])[-128 * 1024:]
            else:
                self._control = (self._control + clean + b"\n")[-128 * 1024:]
        clean = _ANSI.sub(b"", self._pending)
        log = _SYSLOG.search(clean)
        if self._dropping_log:
            self._pending = b""
        elif log:
            self._control = (self._control + clean[:log.start()])[-128 * 1024:]
            self._pending = b""
            self._dropping_log = True
        elif len(self._pending) > 128 * 1024:
            self._pending = self._pending[-128 * 1024:]

    @property
    def data(self) -> bytes:
        """返回含末尾未换行提示符的识别视图，仍保持固定大小上限。"""
        return (self._control + _ANSI.sub(b"", self._pending))[-128 * 1024:]
