"""验收器自身需识别摘要损坏，并接受合法的跨小时半行与文件轮转。"""

import hashlib
import io
import json
import runpy
import tarfile
import zipfile
from pathlib import Path

import pytest

verifier = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/verify_container_service.py"))
PREFIX = b"[2026-09-08 13:59:59] "


def packed(raw, sequence, wrong_digest=False):
    """构造不含实际设备数据的归档与完整性清单。"""
    output = io.BytesIO()
    metadata = {"rawSize": len(raw), "sha256": "invalid" if wrong_digest else hashlib.sha256(raw).hexdigest(),
        "hourStart": "2026-09-08T05:00:00+00:00", "firstSequence": sequence}
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, data in (("output.log", raw), ("manifest.json", json.dumps(metadata).encode())):
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


def test_archive_verifier_orders_fragments_and_rejoins_split_lines():
    output = io.BytesIO()
    first, second = PREFIX + b"fir", b"st\n" + PREFIX + b"second\n"
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("a-second.tar.gz", packed(second, 2))
        bundle.writestr("z-first.tar.gz", packed(first, 1))
    digest = verifier["verify_archive"](output.getvalue(), [b"first\n", b"second\n"])
    assert digest == hashlib.sha256(first + second).hexdigest()


def test_archive_verifier_rejects_corrupted_manifest():
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
