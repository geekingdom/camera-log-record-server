"""逐路 API 可读延迟的有界统计、慢样本和跨分卷正文回归。"""

import asyncio
import base64
import hashlib
import importlib
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("service_benchmark_latency")


def test_histogram_keeps_slow_samples_without_growing(monkeypatch):
    tracker = module(monkeypatch).BatchLatency()
    for index in range(10000):
        tracker.record(index, index, index + 1)
        tracker.observe(index + 1, index + (.3 if index % 10 == 0 else .1))
    report = tracker.report()
    assert report["samples"] == 10000 and report["batches"] == 10000
    assert report["p99Ms"] > 200
    assert len(tracker.buckets) == 202 and not tracker.pending


def test_pending_overflow_fails_instead_of_dropping_samples(monkeypatch):
    tracker = module(monkeypatch).BatchLatency(max_pending=2)
    tracker.record(0, 0, 1)
    tracker.record(0, 1, 2)
    with pytest.raises(BufferError):
        tracker.record(0, 2, 3)


async def test_catalog_discovery_rechecks_tail_written_after_empty_read(monkeypatch):
    """目录查询期间旧卷补写尾部并发布后继卷，不能把暂时 EOF 当成最终 EOF。"""
    tool = module(monkeypatch)
    body = b"route=0001 seq=000000000 " + b"x" * 39 + b"\n"
    stored = b"[2026-09-09 00:00:00] " + body
    source = SimpleNamespace(route=1, line_bytes=len(body), finished=asyncio.Event(),
                             source_sha256=hashlib.sha256(body).hexdigest())
    source.finished.set()
    tracker = tool.BatchLatency()
    tracker.record(time.monotonic() - 1, 0, 1)
    catalogs, published, old_tail_reads = 0, False, 0

    async def get(path, params=None):
        nonlocal catalogs, published, old_tail_reads
        if path.endswith("log-hours"):
            catalogs += 1
            published = catalogs > 1
            data = {"items": [{"hour": "2026-09-09T00:00:00+00:00", "files":
                               [{"id": "a"}, *([{"id": "b"}] if published else [])]}]}
        else:
            identifier, offset = path.split("/")[-2], params["offset"]
            if identifier == "a":
                raw = stored[:50 if published else 31][offset:]
                old_tail_reads += int(published and offset == 31 and bool(raw))
            else:
                raw = stored[50:][offset:]
            data = {"fileId": identifier, "sessionId": "session", "nextOffset": offset + len(raw),
                    "data": base64.b64encode(raw).decode()}
        return httpx.Response(200, json=data, request=httpx.Request("GET", "http://test" + path))

    result = await tool.observe_read_latency(SimpleNamespace(get=get), "task", source, 1, tracker, 2)
    assert old_tail_reads == 1
    assert result["sourceSha256"] == source.source_sha256
    assert result["samples"] == 1 and result["fileCount"] == 2
    assert result["p99Ms"] >= 1000


@pytest.mark.parametrize("fault", [None, "offset", "session"])
async def test_content_observer_preserves_half_line_across_files(monkeypatch, fault):
    tool = module(monkeypatch)
    body = b"route=0001 seq=000000000 " + b"x" * 39 + b"\n"
    stored = b"[2026-09-09 00:00:00] " + body
    parts = {"a": stored[:31], "b": stored[31:]}
    source = SimpleNamespace(route=1, line_bytes=len(body), finished=asyncio.Event(), source_sha256=hashlib.sha256(body).hexdigest())
    source.finished.set()
    tracker = tool.BatchLatency()
    tracker.record(time.monotonic(), 0, 1)

    async def get(path, params=None):
        if path.endswith("log-hours"):
            data = {"items": [{"hour": "2026-09-09T00:00:00+00:00", "files": [
                {"id": key, "bytes": len(value), "status": "READY"} for key, value in parts.items()]}]}
        else:
            identifier = path.split("/")[-2]
            offset = params["offset"]
            raw = parts[identifier][offset:]
            data = {"fileId": identifier, "sessionId": "session", "nextOffset": offset + len(raw), "data": base64.b64encode(raw).decode()}
            if fault == "offset":
                data["nextOffset"] += 1
            if fault == "session" and identifier == "b":
                data["sessionId"] = "different-session"
        return httpx.Response(200, json=data, request=httpx.Request("GET", "http://test" + path))

    if fault:
        with pytest.raises(AssertionError):
            await tool.observe_read_latency(SimpleNamespace(get=get), "task", source, 1, tracker, 2)
        return
    result = await tool.observe_read_latency(SimpleNamespace(get=get), "task", source, 1, tracker, 2)
    assert result["sourceSha256"] == source.source_sha256
    assert result["sourceLines"] == 1 and result["fileCount"] == 2
    assert result["samples"] == 1
