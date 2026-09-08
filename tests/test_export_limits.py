"""导出产物应在写入前限制实际字节数，并支持大型小时包的 ZIP64。"""

import io
import tarfile
import zipfile

import pytest
from camera_logs.logs.archive_access import copy_limited
from camera_logs.logs.hour_download import write_hour_archive


def test_hour_rebuild_rejects_before_exceeding_limit(tmp_path):
    source, target = tmp_path / "hour.tar.gz", tmp_path / "export.tar.gz"
    raw = b"needle\n" * 100
    with tarfile.open(source, "w:gz") as archive:
        member = tarfile.TarInfo("part-000001.log")
        member.size = len(raw)
        archive.addfile(member, io.BytesIO(raw))
    document = {"id": "file", "bytes": len(raw), "archiveMember": "part-000001.log"}
    with pytest.raises(ValueError, match="export output"):
        write_hour_archive(target, [(document, document, source, True)], max_output_bytes=32)
    assert target.stat().st_size <= 32


def test_copy_rejects_before_exceeding_limit(tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"a" * 128)
    with pytest.raises(ValueError, match="export output"):
        copy_limited(source, target, max_output_bytes=64)
    assert not target.exists() or target.stat().st_size <= 64


def test_zip_counts_directory_and_supports_zip64(tmp_path, monkeypatch):
    from camera_logs.logs.export_output import write_zip

    source = tmp_path / "hour.tar.gz"
    source.write_bytes(b"a" * 128)
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 64)
    target = tmp_path / "hours.zip"
    write_zip(target, [source], 4096)
    with zipfile.ZipFile(target) as archive:
        assert archive.read(source.name) == source.read_bytes()
        assert archive.getinfo(source.name).compress_type == zipfile.ZIP_STORED
    # 正文能放下，但中央目录和 ZIP64 元数据也必须计入上限。
    limit = target.stat().st_size - 1
    with pytest.raises(ValueError, match="export output"):
        write_zip(target, [source], limit)
    assert target.stat().st_size <= limit
