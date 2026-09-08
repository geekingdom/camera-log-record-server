"""跨读取块与连续分卷的字面匹配，尾部状态大小仅由关键词长度决定。"""

import json
from datetime import datetime


def index_entries(stream, cancelled):
    """有界拆分 JSONL 索引；拒绝异常长记录，不一次性展开完整索引。"""
    pending = b""
    while chunk := stream.read(65536):
        if cancelled():
            raise InterruptedError("job cancelled")
        lines = (pending + chunk).split(b"\n")
        pending = lines.pop()
        for line in lines:
            if len(line) > 16384:
                raise ValueError("日志索引单条记录过长")
            if line:
                yield json.loads(line)
        if len(pending) > 16384:
            raise ValueError("日志索引单条记录过长")
    if pending:
        yield json.loads(pending)


class IndexCursor:
    """按递增正文偏移消费索引，仅保留当前及下一条记录。"""

    def __init__(self, entries):
        self.entries = iter(entries)
        self.current = None
        self.pending = next(self.entries, None)

    def locate(self, position):
        while self.pending is not None and self.pending["offset"] <= position:
            self.current = self.pending
            self.pending = next(self.entries, None)
        if self.current and position < self.current["offset"] + self.current["length"]:
            return self.current
        return None


class StreamSearch:
    """单作业专用匹配器，以起始字节的文件、偏移和接收时间归属结果。

    每个尾部字节携带原始位置，支持关键词跨越多个短分卷。只有相同采集身份
    且索引块序号连续才跨文件保留；索引缺失或冻结在块中间时不推测连续性。
    """

    def __init__(self, needle: bytes, start: datetime, end: datetime):
        self.needle, self.start, self.end = needle, start, end
        self.tail, self.origins = b"", []
        self.previous_identity, self.previous_sequence = None, None

    def scan(self, stream, index, file, cancelled):
        """读取一个固定水位文件，连续文件间仅保存关键词所需的未完成尾部。"""
        identity = tuple(file.get(key) for key in ("taskId", "runId", "sessionId", "nodeId"))
        cursor = IndexCursor(index)
        first = cursor.pending
        continuous = (all(identity) and identity == self.previous_identity and first
            and first.get("offset") == 0 and self.previous_sequence is not None
            and first.get("sequence") == self.previous_sequence + 1)
        if not continuous:
            self.tail, self.origins = b"", []
        def origin(position):
            entry = cursor.locate(position)
            stamp = datetime.fromisoformat(entry["receivedAt"]) if entry else None
            return file.get("id"), position, stamp

        offset = 0
        while chunk := stream.read(256 * 1024):
            if cancelled():
                raise InterruptedError("job cancelled")
            data, tail_size = self.tail + chunk, len(self.tail)
            begin = 0
            while (found := data.find(self.needle, begin)) >= 0:
                file_id, position, stamp = (self.origins[found] if found < tail_size
                                            else origin(offset + found - tail_size))
                if stamp is None or self.start <= stamp <= self.end:
                    yield {"fileId": file_id, "offset": position,
                        "text": data[max(0, found - 120):found + len(self.needle) + 120].decode("utf-8", "replace")}
                begin = found + len(self.needle)
            keep = min(len(data), len(self.needle) - 1)
            if keep:
                retained = max(0, keep - len(chunk))
                self.origins = (self.origins[-retained:] if retained else []) + [
                    origin(offset + position) for position in range(max(0, len(chunk) - keep), len(chunk))]
                self.tail = data[-keep:]
            else:
                self.tail, self.origins = b"", []
            offset += len(chunk)
        self.previous_identity = identity
        # 快照可能包含水位之后追加的索引；仅完全覆盖末块才能接续下个文件。
        last = cursor.locate(offset - 1) if offset else None
        self.previous_sequence = (last.get("sequence") if last and cursor.pending is None
            and last["offset"] + last["length"] == offset else None)
