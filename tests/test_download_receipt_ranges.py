"""下载重组的接收时间索引可信边界回归。"""

import io
import tarfile
from pathlib import Path

from camera_logs.logs.hour_download import MAX_INDEX_LINE_BYTES, write_hour_archive


def _archive(path: Path, body: bytes, indexes: dict[str, bytes]) -> None:
    """建立只含一个日志成员和指定内部索引成员的冻结快照。"""
    with tarfile.open(path, "w:gz") as bundle:
        info = tarfile.TarInfo("source.log"); info.size = len(body)
        bundle.addfile(info, io.BytesIO(body))
        for name, content in indexes.items():
            info = tarfile.TarInfo(name); info.size = len(content)
            bundle.addfile(info, io.BytesIO(content))


def _names(output: Path) -> list[str]:
    with tarfile.open(output, "r:gz") as bundle:
        return bundle.getnames()


def _source(path: Path, index_path: Path) -> tuple[dict, dict, Path, bool]:
    return ({"id": "file", "bytes": 3}, {
        "id": "file", "taskId": "task", "ip": "192.0.2.10", "archiveMember": "source.log",
        "indexPath": str(index_path),
    }, path, True)


def test_rebuild_does_not_fallback_to_current_external_or_unrelated_embedded_index(tmp_path):
    """缺少精确固定索引时，远端同名路径和包内其它索引均不得参与命名。"""
    source, output = tmp_path / "source.tar.gz", tmp_path / "output.tar.gz"
    external = tmp_path / "expected.index.jsonl"
    external.write_text('{"offset":0,"length":3,"receivedAt":"2026-10-19T12:18:41+08:00"}\n', encoding="utf-8")
    _archive(source, b"abc", {
        "unrelated.index.jsonl": b'{"offset":0,"length":3,"receivedAt":"2026-10-19T12:19:42+08:00"}\n',
    })

    write_hour_archive(output, [_source(source, external)])

    assert _names(output) == ["task-192.0.2.10-unknown-time-part-000001.log"]


def test_rebuild_limits_index_line_length_before_json_parsing(tmp_path):
    """损坏索引的超长单行只降级为未知时间，不能无界分配内存。"""
    source, output = tmp_path / "source.tar.gz", tmp_path / "output.tar.gz"
    index = tmp_path / "expected.index.jsonl"
    long_line = b"{" + b"x" * (MAX_INDEX_LINE_BYTES + 1)
    _archive(source, b"abc", {index.name: long_line})

    write_hour_archive(output, [_source(source, index)])

    assert _names(output) == ["task-192.0.2.10-unknown-time-part-000001.log"]


def test_rebuild_rejects_receipt_time_without_timezone(tmp_path):
    """没有时区的接收时间缺少绝对语义，只能导出 unknown-time。"""
    source, output = tmp_path / "source.tar.gz", tmp_path / "output.tar.gz"
    index = tmp_path / "expected.index.jsonl"
    _archive(source, b"abc", {index.name: b'{"offset":0,"length":3,"receivedAt":"2026-10-19T12:18:41"}\n'})

    write_hour_archive(output, [_source(source, index)])

    assert _names(output) == ["task-192.0.2.10-unknown-time-part-000001.log"]


def test_rebuild_does_not_read_index_after_exact_frozen_boundary(tmp_path):
    """分卷恰在索引记录结束时，水位后的坏行不能影响已冻结正文的时间名。"""
    source, output = tmp_path / "source.tar.gz", tmp_path / "output.tar.gz"
    index = tmp_path / "expected.index.jsonl"
    _archive(source, b"abc", {index.name: (
        b'{"offset":0,"length":3,"receivedAt":"2026-10-19T12:18:41+08:00"}\n'
        b"{not-json-after-waterline"
    )})

    write_hour_archive(output, [_source(source, index)])

    assert _names(output) == ["task-192.0.2.10-20261019121841-20261019121841-part-000001.log"]
