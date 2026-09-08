"""合成压测验收必须接受跨归档半行，并识别重复、缺失和跨路混写。"""

import hashlib
import io
import runpy
import tarfile
from pathlib import Path
from types import SimpleNamespace

import pytest

benchmark = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/benchmark_collector.py"))
PREFIX = b"[2026-09-08 13:59:59] "


def archive(tmp_path, raw, sequence):
    """生成只有合成正文的归档，模拟写入器发布的摘要信息。"""
    path = tmp_path / f"{sequence}.tar.gz"
    with tarfile.open(path, "w:gz") as bundle:
        member = tarfile.TarInfo("output.log")
        member.size = len(raw)
        bundle.addfile(member, io.BytesIO(raw))
    return SimpleNamespace(path=path, first_sequence=sequence, sha256=hashlib.sha256(raw).hexdigest())


def test_benchmark_accepts_line_and_timestamp_split_across_archives(tmp_path):
    source = benchmark["line"](0, 0, 256) + benchmark["line"](0, 1, 256)
    stored = PREFIX + source[:256] + PREFIX + source[256:]
    # 第一处分段位于时间前缀内，第二处分段位于日志正文内；回调顺序刻意打乱。
    fragments = [archive(tmp_path, stored[:9], 1), archive(tmp_path, stored[9:333], 2),
                 archive(tmp_path, stored[333:], 3)]
    digest, size, compressed = benchmark["verify_route_archives"](list(reversed(fragments)))
    assert digest == hashlib.sha256(source).hexdigest()
    assert size == len(stored)
    assert compressed == sum(item.path.stat().st_size for item in fragments)


@pytest.mark.parametrize("sequences", [[0, 0, 1], [1], [1, 0]])
def test_benchmark_digest_detects_duplicate_missing_and_reordered_lines(tmp_path, sequences):
    expected = b"".join(benchmark["line"](0, seq, 256) for seq in [0, 1])
    raw = b"".join(PREFIX + benchmark["line"](0, seq, 256) for seq in sequences)
    digest, _, _ = benchmark["verify_route_archives"]([archive(tmp_path, raw, 1)])
    assert digest != hashlib.sha256(expected).hexdigest()


def test_benchmark_digest_detects_cross_route_data(tmp_path):
    raw = PREFIX + benchmark["line"](1, 0, 256)
    digest, _, _ = benchmark["verify_route_archives"]([archive(tmp_path, raw, 1)])
    assert digest != hashlib.sha256(benchmark["line"](0, 0, 256)).hexdigest()


@pytest.mark.parametrize("raw", [b"missing-prefix\n", PREFIX + b"unfinished"])
def test_benchmark_rejects_missing_prefix_and_truncated_final_line(tmp_path, raw):
    with pytest.raises(AssertionError):
        benchmark["verify_route_archives"]([archive(tmp_path, raw, 1)])


def test_benchmark_rejects_archive_digest_corruption(tmp_path):
    item = archive(tmp_path, PREFIX + b"complete\n", 1)
    item.sha256 = "invalid"
    with pytest.raises(AssertionError):
        benchmark["verify_route_archives"]([item])
