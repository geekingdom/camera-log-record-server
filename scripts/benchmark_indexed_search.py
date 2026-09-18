"""验证小时归档候选索引的精确性与实际扫描范围。

所有样本均生成在临时目录。索引只规划候选，结果一律由 ``StreamSearch``
重新扫描原始 tar.gz 成员得出，避免把 FTS 命中当作用户可见搜索结果。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import tarfile
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from camera_logs.logs.search_archives import ReadyArchiveGroup
from camera_logs.logs.search_index import build_index, index_path, query_index
from camera_logs.logs.search_plan import plan_candidate_files
from camera_logs.logs.search_stream import StreamSearch

MEBIBYTE = 1024 * 1024
DEFAULT_HOURS = 1
DEFAULT_MEMBERS_PER_HOUR = 60
DEFAULT_MEMBER_BYTES = 10_000_000
QUERIES = ("sparseunique", "不存在的关键词", "任意中文子串", "跨段长行命中")


def _index_file(path: Path, length: int, sequence: int) -> None:
    """写入单段覆盖索引，使连续成员与正式流式检索使用相同身份和序号语义。"""
    entry = {
        "offset": 0,
        "length": length,
        "receivedAt": datetime(2026, 9, 18, tzinfo=UTC).isoformat(),
        "sequence": sequence,
        "taskId": "benchmark-task",
        "runId": "benchmark-run",
        "sessionId": "benchmark-session",
        "nodeId": "benchmark-node",
    }
    path.write_text(json.dumps(entry) + "\n", encoding="utf-8")


def _regular_member(size: int, sequence_start: int, marker: bytes = b"") -> bytes:
    """构造典型 256 字节 UTF-8 行，含组件、级别、递增序号与随机散列十六进制。"""
    payload = bytearray()
    levels = (b"INFO", b"WARN", b"DEBUG", b"ERROR")
    sequence = sequence_start
    while len(payload) < size:
        digest = hashlib.blake2s(str(sequence).encode(), digest_size=32).hexdigest().encode()
        prefix = b"2026-09-18 08:00:00.000 camera.collector " + levels[sequence % len(levels)]
        line = prefix + f" seq={sequence:09d} hex={digest.decode()}".encode()
        payload.extend(line + b" " * (255 - len(line)) + b"\n")
        sequence += 1
    del payload[size:]
    if marker:
        position = min(len(payload) - len(marker) - 2, 8 * 1024)
        payload[position:position + len(marker)] = marker
    return bytes(payload)


def _cross_segment_members(size: int, sequence_start: int) -> list[bytes]:
    """构造两个等长连续成员的跨段长行，命中行不超过正式结果文本上限。"""
    keyword = "跨段长行命中".encode()
    before = b"2026-09-18 08:00:00.000 " + b"a" * 40_000 + keyword[:6]
    first = _regular_member(size - len(before) - 1, sequence_start) + b"\n" + before
    second_head = keyword[6:] + b"b" * 80_000 + b"\n"
    second = second_head + _regular_member(size - len(second_head), sequence_start + 1)
    return [first, second]


def _fixture(root: Path, hours: int, members_per_hour: int, member_bytes: int) -> tuple[list[dict[str, Any]], dict[str, Path]]:
    """生成已知最大压力场景的小时包及 sidecar JSONL 索引，不宣称代表通用负载。"""
    archives: dict[str, Path] = {}
    files: list[dict[str, Any]] = []
    sequence = 1
    for hour in range(hours):
        archive_id = f"hour-{hour:02d}"
        archive = root / f"{archive_id}.tar.gz"
        archives[archive_id] = archive
        members = [
            _regular_member(
                member_bytes,
                (sequence + number) * 100_000,
                (b"sparseunique" if hour == 0 and number == 0
                 else "任意中文子串".encode() if hour == 0 and number == 1 else b""),
            )
            for number in range(members_per_hour)
        ]
        if hour == hours - 1:
            members[-2:] = _cross_segment_members(member_bytes, sequence * 100_000)
        with tarfile.open(archive, "w:gz", compresslevel=1) as bundle:
            for number, payload in enumerate(members):
                member_name = f"{archive_id}-part-{number:02d}.log"
                member = tarfile.TarInfo(member_name)
                member.size = len(payload)
                bundle.addfile(member, io.BytesIO(payload))
                file_id = f"{archive_id}-file-{number:02d}"
                sidecar = root / f"{file_id}.index.jsonl"
                _index_file(sidecar, len(payload), sequence)
                files.append({
                    "id": file_id,
                    "archiveId": archive_id,
                    "taskId": "benchmark-task",
                    "runId": "benchmark-run",
                    "sessionId": "benchmark-session",
                    "nodeId": "benchmark-node",
                    "path": str(archive),
                    "archiveMember": member_name,
                    "indexPath": str(sidecar),
                    "bytes": len(payload),
                    "status": "READY",
                })
                sequence += 1
    return files, archives


def _scan(files: list[dict[str, Any]], archives: dict[str, Path], keyword: str) -> tuple[list[dict[str, Any]], int]:
    """按冻结文件顺序扫描原始成员；返回结果及实际打开的小时归档数量。"""
    start = datetime(2026, 9, 18, tzinfo=UTC) - timedelta(minutes=1)
    scanner = StreamSearch(keyword.encode(), start, start + timedelta(minutes=2))
    results: list[dict[str, Any]] = []
    opened = 0
    current_archive_id: str | None = None
    group: ReadyArchiveGroup | None = None
    try:
        for file in files:
            archive_id = str(file["archiveId"])
            if archive_id != current_archive_id:
                if group is not None:
                    group.close()
                group = ReadyArchiveGroup(archives[archive_id], scanner, lambda: False)
                group.open()
                opened += 1
                current_archive_id = archive_id
            found, _text_bytes, bounded = group.scan_member(
                file, Path(file["indexPath"]), 1000 - len(results), 8 * MEBIBYTE, lambda: False
            )
            if bounded:
                raise AssertionError("基准结果达到预算，无法比较完整性")
            results.extend(found)
    finally:
        if group is not None:
            group.close()
    results.extend(scanner.finish())
    return results, opened


def _planned_files(files: list[dict[str, Any]], archives: dict[str, Path], keyword: str) -> list[dict[str, Any]]:
    """收集每包索引证据并调用规划器；缺失成员证据保留为未知以保证回退。"""
    evidence: dict[str, dict[str, Any] | None] = {}
    for archive_id, archive in archives.items():
        found = query_index(archive, keyword)
        for file in files:
            if file["archiveId"] == archive_id:
                evidence[str(file["id"])] = None if found is None else found.get(file["archiveMember"])
    selected = plan_candidate_files(files, evidence, keyword)
    return [file for file in files if file["id"] in selected]


def _measure(operation):
    """记录操作耗时，同时让调用方保留可 JSON 化的原始结果。"""
    started = time.perf_counter()
    value = operation()
    return value, time.perf_counter() - started


def main() -> None:
    """运行构建、首查/复查及四类原文等价性比较，并验证临时目录已自动回收。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=DEFAULT_HOURS)
    parser.add_argument("--members-per-hour", type=int, default=DEFAULT_MEMBERS_PER_HOUR)
    parser.add_argument("--member-bytes", type=int, default=DEFAULT_MEMBER_BYTES)
    parser.add_argument("--report-path", type=Path)
    arguments = parser.parse_args()
    if arguments.hours < 1 or arguments.members_per_hour < 3 or arguments.member_bytes < MEBIBYTE:
        raise ValueError("hours 至少为 1，members-per-hour 至少为 3，member-bytes 至少为 1MiB")

    temporary: str | None = None
    report: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix="camera-indexed-search-") as directory:
        temporary = directory
        root = Path(directory)
        files, archives = _fixture(root, arguments.hours, arguments.members_per_hour, arguments.member_bytes)
        raw_bytes = sum(int(file["bytes"]) for file in files)
        _, build_seconds = _measure(lambda: [
            build_index(archive) or (_ for _ in ()).throw(RuntimeError(f"索引构建失败: {archive}"))
            for archive in archives.values()
        ])
        index_bytes = sum(index_path(archive).stat().st_size for archive in archives.values())
        cases = []
        for keyword in QUERIES:
            (baseline, baseline_archives), baseline_seconds = _measure(
                lambda query=keyword: _scan(files, archives, query)
            )
            planned, first_query_seconds = _measure(
                lambda query=keyword: _planned_files(files, archives, query)
            )
            planned_again, repeated_query_seconds = _measure(
                lambda query=keyword: _planned_files(files, archives, query)
            )
            if [file["id"] for file in planned] != [file["id"] for file in planned_again]:
                raise AssertionError("重复索引查询得到不稳定候选集")
            (candidate, candidate_archives), candidate_seconds = _measure(
                lambda subset=planned, query=keyword: _scan(subset, archives, query)
            )
            if candidate != baseline:
                raise AssertionError(f"{keyword!r} 候选扫描与全量原文扫描结果不等价")
            cases.append({
                "keyword": keyword,
                "baselineSeconds": round(baseline_seconds, 6),
                "candidateScanSeconds": round(candidate_seconds, 6),
                "firstQuerySeconds": round(first_query_seconds, 6),
                "repeatedQuerySeconds": round(repeated_query_seconds, 6),
                "baselineArchivesScanned": baseline_archives,
                "candidateArchivesScanned": candidate_archives,
                "candidateFiles": len(planned),
                "resultCount": len(candidate),
                "exactResultEquivalent": True,
            })
        report = {
            "hours": arguments.hours,
            "membersPerHour": arguments.members_per_hour,
            "files": len(files),
            "rawArchiveMemberBytes": raw_bytes,
            "scenario": (
                "已知单设备最大压力场景，不代表通用生产容量"
                if (arguments.hours == DEFAULT_HOURS
                    and arguments.members_per_hour == DEFAULT_MEMBERS_PER_HOUR
                    and arguments.member_bytes == DEFAULT_MEMBER_BYTES)
                else "多小时合成候选跳过验证，不代表生产容量"
            ),
            "largerThan128MiB": raw_bytes > 128 * MEBIBYTE,
            "indexBuildSeconds": round(build_seconds, 6),
            "indexBytes": index_bytes,
            "cases": cases,
        }
    if temporary is None or Path(temporary).exists():
        raise AssertionError("基准临时目录未被完整清理")
    report["temporaryDataCleaned"] = True
    rendered = json.dumps(report, ensure_ascii=False)
    if arguments.report_path is not None:
        target = arguments.report_path
        temporary_report = target.with_name(target.name + ".partial")
        temporary_report.write_text(rendered + "\n", encoding="utf-8")
        os.replace(temporary_report, target)
    print(rendered)


if __name__ == "__main__":
    main()
