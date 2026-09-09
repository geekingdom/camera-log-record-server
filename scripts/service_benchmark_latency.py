"""负载期间逐路读取正式内容 API，按完整批次测量行级可读延迟上界。"""

import asyncio
import base64
import hashlib
import math
import time
from collections import deque

from service_benchmark_realtime import _complete_line


class BatchLatency:
    """保留有限未完成批次与固定直方图；超过 200ms 的样本不能被淘汰。"""

    def __init__(self, max_pending=1000):
        self.pending = deque()
        self.max_pending = max_pending
        self.buckets = [0] * 202
        self.samples = self.batches = self.lines = 0
        self.completed_at = None
        self.maximum_ms = 0.0

    def record(self, started, before, after):
        """发送回调可能晚于读取；入队后立即尝试用已读水位结算。"""
        if len(self.pending) >= self.max_pending:
            raise BufferError("内容 API 未完成批次超过有界缓冲")
        self.pending.append((started, before, after))
        self.observe(self.lines, self.completed_at)

    def observe(self, lines, completed_at):
        """将批次最后一行可见的时刻用于整批行的保守延迟统计。"""
        self.lines, self.completed_at = lines, completed_at
        while self.pending and completed_at is not None and lines >= self.pending[0][2]:
            started, before, after = self.pending.popleft()
            milliseconds = max(0.0, (completed_at - started) * 1000)
            self.buckets[min(201, math.ceil(milliseconds))] += after - before
            self.samples += after - before
            self.batches += 1
            self.maximum_ms = max(self.maximum_ms, milliseconds)

    def report(self):
        """1ms 桶给出百分位上界；溢出桶用实际最大值保守报告，不压低慢样本。"""
        if not self.samples or self.pending:
            raise AssertionError("内容 API 延迟样本为空或尚未全部完成")
        rank = math.ceil(self.samples * .99)
        accumulated = 0
        for index, count in enumerate(self.buckets):
            accumulated += count
            if accumulated >= rank:
                p99 = self.maximum_ms if index == 201 else index
                break
        return {"samples": self.samples, "batches": self.batches, "p99Ms": p99,
                "maxMs": self.maximum_ms, "within200Ms": p99 <= 200}


async def observe_read_latency(client, task_id, source, expected_lines, tracker, timeout):
    """跨小时和分卷续读并比对正文摘要；HTTP 轮询和排队均包含在计时中。"""
    deadline = time.monotonic() + timeout
    current, session, offset = None, None, 0
    queued, seen = deque(), set()
    next_catalog = 0.0
    pending, lines, file_count = b"", 0, 0
    digest = hashlib.sha256()
    while time.monotonic() < deadline:
        if current is None:
            data = b""
        else:
            response = await client.get(f"/api/v1/log-files/{current}/content", params={"offset": offset, "limit": 262144})
            response.raise_for_status()
            body = response.json()
            data = base64.b64decode(body["data"], validate=True)
            if body.get("fileId") != current or type(body.get("nextOffset")) is not int or body["nextOffset"] != offset + len(data):
                raise AssertionError("内容 API 文件身份或字节偏移不连续")
            if not isinstance(body.get("sessionId"), str) or not body["sessionId"]:
                raise AssertionError("内容 API 缺少会话身份")
            if session is None:
                session = body.get("sessionId")
            elif body.get("sessionId") != session:
                raise AssertionError("内容 API 读取期间会话发生变化")
            offset += len(data)
        if data:
            received_at = time.monotonic()
            complete = (pending + data).split(b"\n")
            pending = complete.pop()
            if len(pending) > source.line_bytes + 22:
                raise AssertionError("内容 API 半行超过长度上限")
            for raw in complete:
                if lines >= expected_lines:
                    raise AssertionError("内容 API 返回额外行")
                _complete_line(raw, route=source.route, line_bytes=source.line_bytes, sequence=lines, digest=digest)
                lines += 1
            tracker.observe(lines, received_at)
        if source.finished.is_set() and lines == expected_lines:
            if pending or digest.hexdigest() != source.source_sha256:
                raise AssertionError("内容 API 正文与源摘要不一致")
            return tracker.report() | {"sourceLines": lines, "sourceSha256": digest.hexdigest(), "fileCount": file_count}
        moment = time.monotonic()
        if not data and moment >= next_catalog:
            response = await client.get(f"/api/v1/tasks/{task_id}/log-hours")
            response.raise_for_status()
            discovered = False
            for hour in sorted(response.json()["items"], key=lambda item: item["hour"]):
                for file in hour["files"]:
                    if file["id"] not in seen:
                        seen.add(file["id"])
                        queued.append(file["id"])
                        discovered = True
            next_catalog = moment + .1
            if current is not None and discovered:
                # 查询目录期间旧卷可能刚补完尾部并封存；发现新卷后必须再读旧卷，
                # 不能依据查询之前的暂时 EOF 跳过这段字节。原延迟计时不重置。
                continue
        if not data and queued:
            current, offset = queued.popleft(), 0
            file_count += 1
            continue
        await asyncio.sleep(.01)
    raise TimeoutError("内容 API 未在限定时间内收齐日志")
