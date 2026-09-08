"""设备日志行时间戳前缀转换器。

转换器只维护“下一字节是否属于新行”的极小状态，不缓存整行。因此超长行和跨
网络分包日志不会导致内存增长；每个会话创建独立实例，不能与其他任务混用。
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


class LinePrefixer:
    """为每个 LF/CRLF 逻辑行添加按首字节接收时间生成的服务器时间戳。"""

    def __init__(self, timezone: str = "Asia/Shanghai") -> None:
        self._zone = ZoneInfo(timezone)
        self._at_line_start = True

    def prefix(self, data: bytes, received_at: datetime) -> bytes:
        """流式处理一个接收块，保留正文、换行和重复设备输出的全部原始字节。"""
        if not data:
            return data
        stamp = received_at.astimezone(self._zone).strftime("[%Y-%m-%d %H:%M:%S] ").encode()
        was_at_line_start = self._at_line_start
        # bytes.replace 在 C 层处理整块，避免高吞吐采集任务执行逐字节 Python 循环。
        output = data.replace(b"\n", b"\n" + stamp)
        if data.endswith(b"\n"):
            # 最后一行已经结束，不为尚未到达的下一行提前写入时间戳。
            output = output[:-len(stamp)]
            self._at_line_start = True
        else:
            self._at_line_start = False
        return (stamp if was_at_line_start else b"") + output
