"""跨读取块与连续分卷的逐行字面检索，结果绝不混入相邻日志行。"""

import json
from datetime import datetime

# 单条完整行需要进入搜索作业的 Mongo 结果文档；超过该边界无法保证既完整又有界。
MAX_RESULT_TEXT_BYTES = 256 * 1024


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
    """单作业逐行匹配器，以首个有效命中位置归属完整日志行。

    匹配尾部仅保留关键词长度，完整行正文最多保留 ``MAX_RESULT_TEXT_BYTES``。
    超过边界的非命中行继续流式扫描后丢弃；若该行命中，作业明确失败而不返回
    被截断后却声称完整的文本。连续文件仅在身份和索引序号均连续时共享未结束行。
    """

    def __init__(self, needle: bytes, start: datetime, end: datetime):
        if not needle:
            raise ValueError("关键词不能为空")
        self.needle, self.start, self.end = needle, start, end
        self.tail, self.origins = b"", []
        self.previous_identity, self.previous_sequence = None, None
        self.line, self.line_bytes, self.line_overflow = bytearray(), 0, False
        self.line_origin = None
        self.match = None

    def _clear_line(self):
        """清空已完成逻辑行的正文、关键词尾部和归属状态。"""
        self.tail, self.origins = b"", []
        self.line.clear()
        self.line_bytes, self.line_overflow = 0, False
        self.line_origin, self.match = None, None

    def _complete_line(self):
        """在换行、连续性中断或作业结束时提交已命中的一整行。"""
        try:
            if self.match is None:
                return
            if self.line_overflow:
                raise ValueError(f"命中日志行超过 {MAX_RESULT_TEXT_BYTES} 字节，无法完整保存")
            payload = bytes(self.line)
            if payload.endswith(b"\r"):
                payload = payload[:-1]
            try:
                text, encoding_error = payload.decode("utf-8"), False
            except UnicodeDecodeError:
                # 原始日志仍在文件中按字节保存；搜索展示层沿用替换解码，并显式标记，
                # 不能把设备的非 UTF-8 输出变成整项搜索作业失败。
                text, encoding_error = payload.decode("utf-8", "replace"), True
            file_id, offset, _stamp = self.match
            line_file_id, line_offset, _line_stamp = self.line_origin
            result = {
                "fileId": file_id,
                "offset": offset,
                "lineStartFileId": line_file_id,
                "lineStartOffset": line_offset,
                "text": text,
            }
            if encoding_error:
                result["encodingError"] = True
            yield result
        finally:
            self._clear_line()

    def finish(self):
        """结束全部连续文件后提交无换行结尾的最后逻辑行。"""
        yield from self._complete_line()

    def _append_segment(self, segment, segment_offset, origin):
        """处理当前行的一个无换行片段，并保留跨块匹配所需短尾部。"""
        if not segment:
            return
        if self.line_origin is None:
            self.line_origin = origin(segment_offset)
        self.line_bytes += len(segment)
        if not self.line_overflow:
            remaining = MAX_RESULT_TEXT_BYTES - len(self.line)
            self.line.extend(segment[:max(0, remaining)])
            self.line_overflow = len(segment) > remaining

        previous_tail, previous_origins = self.tail, self.origins
        data = previous_tail + segment
        if self.match is None:
            found = 0
            while (found := data.find(self.needle, found)) >= 0:
                source = (previous_origins[found] if found < len(previous_tail)
                          else origin(segment_offset + found - len(previous_tail)))
                # 旧归档可能没有旁路索引；保持既有兼容语义，在这种情况下不能
                # 伪造接收时间，也不能把所有历史关键词搜索结果静默丢弃。
                if source[2] is None or self.start <= source[2] < self.end:
                    self.match = source
                    break
                found += len(self.needle)

        keep = min(len(data), len(self.needle) - 1)
        if not keep:
            self.tail, self.origins = b"", []
            return
        retained_from = len(data) - keep
        tail, origins = bytearray(), []
        if retained_from < len(previous_tail):
            tail.extend(previous_tail[retained_from:])
            origins.extend(previous_origins[retained_from:])
            source_start = 0
        else:
            source_start = retained_from - len(previous_tail)
        tail.extend(segment[source_start:])
        origins.extend(origin(segment_offset + item) for item in range(source_start, len(segment)))
        self.tail, self.origins = bytes(tail), origins

    def scan(self, stream, index, file, cancelled):
        """扫描固定水位文件；仅连续分卷保留未结束行和关键词尾部。"""
        identity = tuple(file.get(key) for key in ("taskId", "runId", "sessionId", "nodeId"))
        cursor = IndexCursor(index)
        first = cursor.pending
        continuous = (all(identity) and identity == self.previous_identity and first
                      and first.get("offset") == 0 and self.previous_sequence is not None
                      and first.get("sequence") == self.previous_sequence + 1)
        if not continuous:
            yield from self._complete_line()

        def origin(position):
            entry = cursor.locate(position)
            stamp = datetime.fromisoformat(entry["receivedAt"]) if entry else None
            return file.get("id"), position, stamp

        offset = 0
        while chunk := stream.read(256 * 1024):
            if cancelled():
                raise InterruptedError("job cancelled")
            start = 0
            while True:
                newline = chunk.find(b"\n", start)
                end = len(chunk) if newline < 0 else newline
                self._append_segment(chunk[start:end], offset + start, origin)
                if newline < 0:
                    break
                yield from self._complete_line()
                start = newline + 1
            offset += len(chunk)
        self.previous_identity = identity
        # 快照可能包含水位之后追加的索引；仅完全覆盖末块才能接续下个文件。
        last = cursor.locate(offset - 1) if offset else None
        self.previous_sequence = (last.get("sequence") if last and cursor.pending is None
                                  and last["offset"] + last["length"] == offset else None)
