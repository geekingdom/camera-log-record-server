"""按索引接收时间返回有界原始字节片段，供第三方无关键词查询。"""

import base64
from datetime import datetime


class TimeRangeScan:
    """半开时间区间[start,end)，以采集接收块的时间判定归属，不解析设备正文时间。

    文本预览可能在UTF-8字符或日志行中间分片；调用方用data的Base64及文件偏移
    无损拼接，不能把预览中的替换字符写回原始日志。每片最多4096字节。
    """
    def __init__(self, start, end):
        self.start, self.end = start, end

    def scan(self, stream, index, file, cancelled):
        """顺序读取索引和正文，拒绝缺失/重叠索引，不用未知时间冒充范围内结果。"""
        position = 0
        for entry in index:
            offset, length = entry["offset"], entry["length"]
            if offset != position or length < 0:
                raise ValueError("时间查询索引不连续")
            stamp = datetime.fromisoformat(entry["receivedAt"])
            if stamp.tzinfo is None:
                raise ValueError("时间查询索引缺少时区")
            remaining = length
            while remaining:
                if cancelled():
                    raise InterruptedError("job cancelled")
                data = stream.read(min(4096, remaining))
                if not data:
                    return  # 冻结水位可以位于最后一条索引块内。
                if self.start <= stamp < self.end:
                    yield {"fileId": file["id"], "offset": position, "length": len(data),
                           "receivedAt": stamp.isoformat(), "data": base64.b64encode(data).decode("ascii"),
                           "text": data.decode("utf-8", "replace")}
                position += len(data)
                remaining -= len(data)
        if stream.read(1):
            raise ValueError("时间查询缺少完整接收时间索引")
