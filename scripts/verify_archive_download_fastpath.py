"""合成验证已归档小时下载的 ZIP STORE 快速路径。

生成 24 个各含三段日志的 READY 小时包，模拟约 504 MiB 原始日志压缩至数十
MiB 的下载输入。主对照比较 HEAD 旧 ``TarFile.getmembers()`` 校验加 ZIP STORE
与真实旁路 metadata 校验加 ZIP STORE；历史重组仅作独立参考。脚本验证 ZIP 的
归档字节与解包正文摘要。所有产物均位于临时目录，不会读取、修改或删除服务日志。
"""

from __future__ import annotations

import hashlib
import json
import tarfile
import tempfile
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from camera_logs.logs.archive_access import gzip_tar_writer, read_limiter
from camera_logs.logs.export_output import write_zip
from camera_logs.logs.hour_download import pin_hour_sources, reusable_hour_archive, write_hour_archive
from camera_logs.logs.naming import segment_filename

HOURS = 24
RAW_BYTES_PER_HOUR = 21 * 1024 * 1024
MEMBERS_PER_HOUR = 3
RAW_BYTES_PER_MEMBER = RAW_BYTES_PER_HOUR // MEMBERS_PER_HOUR
BLOCK_BYTES = 32 * 1024
ENTROPY_BYTES_PER_BLOCK = 1200


class SyntheticLogStream:
    """按块生成可复现的压缩比样本，同时在 tar 写入时累计原始摘要。"""

    def __init__(self, hour: int, member: int, size: int) -> None:
        self.hour, self.member, self.size, self.position = hour, member, size, 0
        self.digest = hashlib.sha256()

    def _block(self, number: int) -> bytes:
        """每块保留有限高熵正文，使压缩比例接近实际小时包而无需原始大文件。"""
        seed = f"archive-download-fastpath:{self.hour}:{self.member}:{number}".encode()
        marker = hashlib.shake_256(seed).digest(ENTROPY_BYTES_PER_BLOCK)
        return marker + b"x" * (BLOCK_BYTES - len(marker))

    def read(self, size: int = -1) -> bytes:
        """实现 tarfile 所需的有限流式读取，不保留已生成的原始正文。"""
        if self.position >= self.size:
            return b""
        remaining = self.size - self.position
        wanted = remaining if size < 0 else min(size, remaining)
        pieces: list[bytes] = []
        while wanted:
            block_number, offset = divmod(self.position, BLOCK_BYTES)
            chunk = self._block(block_number)[offset:offset + wanted]
            pieces.append(chunk)
            self.position += len(chunk)
            wanted -= len(chunk)
        data = b"".join(pieces)
        self.digest.update(data)
        return data


def _create_ready_hour(
    root: Path, hour: int,
) -> tuple[list[tuple[dict, dict, Path, bool]], list[tuple[str, str]]]:
    """创建具有生产旁路 metadata 合同的多分卷 READY 小时包。"""
    archive = root / f"20261012-{hour:02d}.tar.gz"
    members, sources = [], []
    received_at = datetime(2026, 10, 12, hour, tzinfo=UTC)
    with archive.open("wb") as destination, gzip_tar_writer(destination) as bundle:
        for member in range(MEMBERS_PER_HOUR):
            part = member + 1
            source_name = (
                f"task-192.0.2.10-{received_at:%Y%m%d%H%M%S}-"
                f"{received_at + timedelta(hours=1):%Y%m%d%H%M%S}-part-{part:06d}.log"
            )
            name = segment_filename("task", "192.0.2.10", received_at, received_at, part)
            stream = SyntheticLogStream(hour, member, RAW_BYTES_PER_MEMBER)
            info = tarfile.TarInfo(name)
            info.size = RAW_BYTES_PER_MEMBER
            bundle.addfile(info, stream)
            members.append((name, source_name, stream.digest.hexdigest()))
    archive_bytes = archive.stat().st_size
    metadata = {
        "formatVersion": 2,
        "members": [
            {"logName": name, "sourceLogName": source_name, "firstReceivedAt": received_at.isoformat(),
             "lastReceivedAt": received_at.isoformat(), "rawSize": RAW_BYTES_PER_MEMBER, "sha256": digest}
            for name, source_name, digest in members
        ],
    }
    archive.with_name(archive.name + ".metadata.json").write_text(
        json.dumps(metadata, sort_keys=True), encoding="utf-8",
    )
    for member, (name, _source_name, digest) in enumerate(members):
        frozen = {
            "id": f"file-{hour}-{member}", "status": "READY", "bytes": RAW_BYTES_PER_MEMBER,
            "sha256": digest,
        }
        file = {
            "id": frozen["id"], "path": str(archive), "archiveMember": name,
            "archiveGroupId": f"hour-{hour}", "archiveBytes": archive_bytes, "sha256": digest,
        }
        sources.append((frozen, file, archive, False))
    return sources, [(name, digest) for name, _source_name, digest in members]


def _old_reusable_hour_archive(sources: list[tuple[dict, dict, Path, bool]]) -> Path | None:
    """保留 HEAD 旧版完整包确认逻辑，用于与 metadata 快速路径公平对照。"""
    if not sources or any(temporary for _frozen, _file, _path, temporary in sources):
        return None
    paths = {path for _frozen, _file, path, _temporary in sources}
    groups = {str(file.get("archiveGroupId") or "") for _frozen, file, _path, _temporary in sources}
    if len(paths) != 1 or len(groups) != 1 or not next(iter(groups)):
        return None
    path = next(iter(paths))
    if any(frozen.get("status") != "READY" or not file.get("archiveMember")
           for frozen, file, _path, _temporary in sources):
        return None
    try:
        with tarfile.open(path, "r:gz") as archive:
            all_members = archive.getmembers()
            members = [member for member in all_members if member.isfile()]
            if (not members or len(members) != len(all_members)
                    or any(not member.name.endswith(".log") or member.size > 10 * 1024 * 1024
                           for member in members)):
                return None
            selected = {str(file["archiveMember"]) for _frozen, file, _path, _temporary in sources}
            names = [member.name for member in members]
            if len(set(names)) != len(names) or len(selected) != len(sources) or selected != set(names):
                return None
            sizes = {member.name: member.size for member in members}
            return path if all(int(frozen.get("bytes", -1)) == sizes[file["archiveMember"]]
                               for frozen, file, _path, _temporary in sources) else None
    except (OSError, tarfile.TarError):
        return None


def _legacy_rebuild_reference(hours: list[list[tuple[dict, dict, Path, bool]]], output: Path) -> float:
    """测量重组旧成员的历史回退代价，只作参考，不代表本次快速路径的旧实现。"""
    started = time.perf_counter()
    rebuilt = []
    for hour, sources in enumerate(hours):
        destination = output / f"old-hour-{hour:02d}.tar.gz"
        write_hour_archive(destination, sources)
        rebuilt.append((destination, f"hour-{hour:02d}.tar.gz"))
    write_zip(output / "old-hours.zip", rebuilt, limit=10 * 1024 * 1024 * 1024)
    return time.perf_counter() - started


def _zip_elapsed(output: Path, hourly: list[tuple[Path, str]], limit: int) -> float:
    """记录同一 ZIP STORE 输出函数的端到端复制耗时。"""
    started = time.perf_counter()
    write_zip(output, hourly, limit=limit)
    return time.perf_counter() - started


def _verify_zip(output: Path, expected: list[tuple[Path, str, list[tuple[str, str]]]]) -> None:
    """确认 ZIP STORE 没有改写小时包字节，且每小时正文仍符合摘要。"""
    with zipfile.ZipFile(output) as bundle:
        entries = bundle.infolist()
        if len(entries) != len(expected) or any(entry.compress_type != zipfile.ZIP_STORED for entry in entries):
            raise AssertionError("下载 ZIP 未使用一对一 ZIP STORE 小时包")
        for entry, (source, name, raw_digests) in zip(entries, expected, strict=True):
            if entry.filename != name:
                raise AssertionError("下载 ZIP 小时包顺序或名称异常")
            source_bytes = source.read_bytes()
            if bundle.read(entry) != source_bytes:
                raise AssertionError("ZIP 内小时包字节与 READY 来源不一致")
            digest = hashlib.sha256()
            verified = []
            with bundle.open(entry) as packed, tarfile.open(fileobj=packed, mode="r|gz") as archive:
                for member in archive:
                    if not member.isfile() or not member.name.endswith(".log"):
                        raise AssertionError("ZIP 内小时包成员不符合日志合同")
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise AssertionError("ZIP 内小时包正文不可读取")
                    while chunk := stream.read(1024 * 1024):
                        digest.update(chunk)
                    verified.append((member.name, digest.hexdigest()))
                    digest = hashlib.sha256()
            if verified != raw_digests:
                raise AssertionError("ZIP 内小时包正文摘要不一致")


def main() -> None:
    """运行合成归档、计时与完整性验证，并打印可审计的实际规模。"""
    with tempfile.TemporaryDirectory(prefix="camera-log-download-fastpath-") as temporary:
        root = Path(temporary)
        created = [_create_ready_hour(root, hour) for hour in range(HOURS)]
        grouped_sources = [sources for sources, _digests in created]
        pinned = pin_hour_sources(
            [source for sources in grouped_sources for source in sources], root / "pinned",
        )
        pinned_hours = [pinned[offset:offset + MEMBERS_PER_HOUR]
                        for offset in range(0, len(pinned), MEMBERS_PER_HOUR)]
        expected = [(sources[0][2], f"hour-{hour:02d}.tar.gz", digests)
                    for hour, (sources, digests) in enumerate(created)]
        compressed_bytes = sum(source[2].stat().st_size for sources in pinned_hours for source in sources[0:1])

        read_limiter.next_at = 0.0
        old_started = time.perf_counter()
        old_reusable = [_old_reusable_hour_archive(sources) for sources in pinned_hours]
        old_enum_seconds = time.perf_counter() - old_started
        if any(path is None for path in old_reusable):
            raise AssertionError("旧完整包枚举校验未返回可复用归档")
        old_zip_seconds = _zip_elapsed(
            root / "old-fastpath.zip",
            [(path, name) for path, (_source, name, _digest) in zip(old_reusable, expected, strict=True)],
            compressed_bytes + 1024 * 1024,
        )
        old_total_seconds = old_enum_seconds + old_zip_seconds

        original_getmembers = tarfile.TarFile.getmembers

        def forbidden_getmembers(*_args, **_kwargs):
            raise AssertionError("快速路径不得调用 TarFile.getmembers")

        tarfile.TarFile.getmembers = forbidden_getmembers
        try:
            read_limiter.next_at = 0.0
            started = time.perf_counter()
            reusable = [reusable_hour_archive(sources) for sources in pinned_hours]
            fast_metadata_seconds = time.perf_counter() - started
            if any(path is None for path in reusable):
                raise AssertionError("完整 READY 小时包未进入复用快速路径")
            output = root / "hours.zip"
            fast_zip_seconds = _zip_elapsed(
                output,
                [(path, name) for path, (_source, name, _digest) in zip(reusable, expected, strict=True)],
                compressed_bytes + 1024 * 1024,
            )
        finally:
            tarfile.TarFile.getmembers = original_getmembers

        _verify_zip(output, expected)
        read_limiter.next_at = 0.0
        legacy_rebuild_seconds = _legacy_rebuild_reference(pinned_hours, root / "legacy-rebuild")
        raw_bytes = HOURS * RAW_BYTES_PER_HOUR
        print(json.dumps({
            "hours": HOURS,
            "syntheticRawBytes": raw_bytes,
            "syntheticRawMiB": round(raw_bytes / 1024 / 1024, 2),
            "syntheticCompressedBytes": compressed_bytes,
            "syntheticCompressedMiB": round(compressed_bytes / 1024 / 1024, 2),
            "oldGetmembersEnumerationSeconds": round(old_enum_seconds, 3),
            "oldZipStoreSeconds": round(old_zip_seconds, 3),
            "oldTotalSeconds": round(old_total_seconds, 3),
            "fastpathMetadataSeconds": round(fast_metadata_seconds, 3),
            "fastpathZipStoreSeconds": round(fast_zip_seconds, 3),
            "fastpathTotalSeconds": round(fast_metadata_seconds + fast_zip_seconds, 3),
            "legacyRebuildAndZipReferenceSeconds": round(legacy_rebuild_seconds, 3),
            "fastpathGetmembersCalls": 0,
            "zipStoredBytes": output.stat().st_size,
            "verifiedHours": HOURS,
            "temporaryArtifactsCleaned": True,
        }, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
