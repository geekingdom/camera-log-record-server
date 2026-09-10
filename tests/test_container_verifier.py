"""验收器自身需识别摘要损坏，并接受合法的跨小时半行与文件轮转。"""

import asyncio
import hashlib
import io
import runpy
import tarfile
import zipfile
from pathlib import Path

import pytest

verifier = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/verify_container_service.py"))
PREFIX = b"[2026-09-08 13:59:59] "


def packed(raw, sequence, wrong_digest=False):
    """构造按序命名的纯日志归档，损坏注入直接修改正文。"""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in ((f"part-{sequence:06d}.log", raw + b"corrupt" if wrong_digest else raw),):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def test_archive_verifier_orders_fragments_and_rejoins_split_lines():
    output = io.BytesIO()
    first, second = PREFIX + b"fir", b"st\n" + PREFIX + b"second\n"
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("20260908140000.tar.gz", packed(second, 2))
        bundle.writestr("20260908130000.tar.gz", packed(first, 1))
    digest = verifier["verify_archive"](output.getvalue(), [b"first\n", b"second\n"])
    assert digest == hashlib.sha256(first + second).hexdigest()


def test_archive_verifier_rejects_corrupted_log():
    with pytest.raises(AssertionError):
        verifier["verify_archive"](packed(PREFIX + b"first\n", 1, wrong_digest=True), [b"first\n"])


def test_realtime_verifier_accepts_rotation_but_rejects_missing_offsets():
    chunks = {("first-file", "session"): {0: PREFIX + b"fir"},
        ("second-file", "session"): {0: b"st\n"}}
    assert verifier["realtime_lines"](chunks) == [b"first\n"]
    with pytest.raises(AssertionError):
        verifier["realtime_lines"]({("first-file", "session"): {1: PREFIX + b"first\n"}})


def test_timestamp_verifier_rejects_missing_opening_bracket():
    with pytest.raises(AssertionError):
        verifier["strip_prefixed_lines"](b"!2026-09-08 13:59:59] first\n")


def test_realtime_verifier_waits_for_remaining_line_bytes():
    first = PREFIX + b"fir"
    chunks = {("file", "session"): {0: first}}
    assert verifier["realtime_lines"](chunks) == []
    chunks[("file", "session")][len(first)] = b"st\n"
    assert verifier["realtime_lines"](chunks) == [b"first\n"]


def test_resource_cleanup_uses_current_version_and_waits_for_async_deletion(monkeypatch):
    """验收资源必须在任务收尾后软删除，不能遗留地址占用给后续压测。"""
    calls = []

    class Response:
        def __init__(self, body):
            self.body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self.body

    class Client:
        async def get(self, path):
            calls.append(("GET", path))
            return Response({"version": 4})

        async def delete(self, path):
            calls.append(("DELETE", path))
            return Response({"deletionState": "PENDING"})

    async def waited(client, path, predicate, label, timeout):
        calls.append(("WAIT", path, label, timeout))
        assert predicate({"deletionState": "DONE", "activeTaskCount": 0})

    monkeypatch.setitem(verifier["delete_resource"].__globals__, "wait_for", waited)
    asyncio.run(verifier["delete_resource"](Client(), "resource-a", timeout=12))
    assert calls == [
        ("GET", "/api/v1/resources/resource-a"),
        ("DELETE", "/api/v1/resources/resource-a?version=4"),
        ("WAIT", "/api/v1/resources/resource-a", "资源删除", 12),
    ]
