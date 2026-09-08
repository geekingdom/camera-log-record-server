"""设备日志使用的 Telnet 客户端，保留原库协商与背压机制。"""

import datetime
import re

from telnetlib3 import TelnetClient
from telnetlib3.telopt import theNULL


class LogTelnetClient(TelnetClient):
    """为普通日志块提供批量接收路径，控制序列仍由原库处理。"""

    _trigger_key: frozenset[int] | None = None
    _trigger_pattern: re.Pattern[bytes] | None = None

    def _process_chunk(self, data: bytes) -> bool:
        """仅旁路无控制字符的完整块，原库负责所有协商和 SLC 状态转换。"""
        writer = self.writer
        if writer is None or self.reader is None or writer.is_oob:
            return super()._process_chunk(data)
        try:
            mode = writer.mode
        except Exception:  # noqa: BLE001 - 只回退到原库相同的模式异常处理，不吞掉接收异常。
            # 原库对模式读取异常有自己的兼容行为，不能猜测当前协议状态。
            return super()._process_chunk(data)
        if mode != "remote" and not (mode == "kludge" and writer.slc_simulated):
            return super()._process_chunk(data)
        triggers = frozenset({255} | {item.val[0] for item in writer.slctab.values()
                                     if item.val != theNULL})
        if triggers != self._trigger_key:
            # 每连接只保留当前协商表的一个模式；动态 SLC 更新在下一块立即生效。
            self._trigger_pattern = re.compile(b"[" + re.escape(bytes(sorted(triggers))) + b"]")
            self._trigger_key = triggers
        if self._trigger_pattern.search(data) is not None:
            return super()._process_chunk(data)
        # 原库使用无时区 datetime 计算连接 idle，必须保持其时间类型约定。
        self._last_received = datetime.datetime.now()  # noqa: DTZ005
        if data:
            self.reader.feed_data(data)
        return False
