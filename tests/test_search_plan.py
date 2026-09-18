"""全文索引规划必须保守，候选减少不能遗漏跨分卷关键词或完整行。"""

import random
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from camera_logs.logs.search_plan import plan_candidate_files
from camera_logs.logs.search_stream import StreamSearch


def _files(chunks, *, unknown=()):
    files, evidence = [], {}
    for number, chunk in enumerate(chunks):
        identifier = f"file-{number}"
        files.append({"id": identifier, "status": "READY", "bytes": len(chunk)})
        if number not in unknown:
            evidence[identifier] = {"bytes": len(chunk), "head": chunk[:16], "tail": chunk[-16:],
                                    "hasNewline": b"\n" in chunk, "mayMatch": b"nee" in chunk}
    return files, evidence


def test_plan_keeps_cross_file_keyword_and_complete_long_line():
    files, evidence = _files([b"prefix nee", b"dle long line", b" tail\nignored"])
    selected = plan_candidate_files(files, evidence, "needle")
    assert selected == {"file-0", "file-1", "file-2"}


def test_plan_handles_many_tiny_files_and_chinese_utf8_boundary():
    files, evidence = _files(["前".encode(), "缀检".encode(), "索尾\n".encode()])
    for item in evidence.values():
        item["mayMatch"] = False
    selected = plan_candidate_files(files, evidence, "前缀检索")
    assert selected == {"file-0", "file-1", "file-2"}


@pytest.mark.parametrize("keyword", ["ab", "一", "a\nb", "a\0bc", b"\xff\xff\xff"])
def test_plan_falls_back_to_all_for_unindexable_keyword(keyword):
    files, evidence = _files([b"first", b"second"])
    assert plan_candidate_files(files, evidence, keyword) == {"file-0", "file-1"}


def test_plan_keeps_unknown_and_empty_file_boundaries():
    files, evidence = _files([b"", b"nee", b"dle"], unknown=(0,))
    evidence["file-1"]["mayMatch"] = False
    evidence["file-2"]["mayMatch"] = False
    assert plan_candidate_files(files, evidence, "needle") == {"file-0", "file-1", "file-2"}


def test_sparse_match_only_adds_its_immediate_newline_context():
    chunks = [b"line-%d\n" % number for number in range(10)]
    chunks[5] = b"needle\n"
    files, evidence = _files(chunks)
    evidence["file-5"]["mayMatch"] = True
    assert plan_candidate_files(files, evidence, "needle") == {"file-4", "file-5", "file-6"}


def test_missing_identifier_disables_index_exclusion_without_mispaired_evidence():
    files, evidence = _files([b"ignored", b"needle\n"])
    files.insert(1, {"status": "READY", "bytes": 0})
    assert plan_candidate_files(files, evidence, "needle") == {"file-0", "file-1"}


def test_subset_stream_search_matches_full_baseline_with_cross_file_line_origin():
    """候选子集按原冻结顺序扫描，必须保留完整结果及 lineStartFileId。"""
    chunks = [b"line nee", b"dle tail\n", b"unrelated\n"]
    files, evidence = _files(chunks)
    selected = plan_candidate_files(files, evidence, "needle")
    stamp = datetime(2026, 9, 18, tzinfo=UTC)

    def scan(items):
        scanner, results = StreamSearch(b"needle", stamp - timedelta(seconds=1), stamp + timedelta(seconds=1)), []
        for number, item in enumerate(items):
            payload = chunks[int(item["id"].removeprefix("file-"))]
            indexed = [{"offset": 0, "length": len(payload), "receivedAt": stamp.isoformat(), "sequence": number + 1}]
            item = item | {"taskId": "task", "runId": "run", "sessionId": "session", "nodeId": "node"}
            results.extend(scanner.scan(BytesIO(payload), indexed, item, lambda: False))
        results.extend(scanner.finish())
        return results

    full = scan(files)
    subset = scan([item for item in files if item["id"] in selected])
    assert subset == full == [{"fileId": "file-0", "offset": 5, "lineStartFileId": "file-0",
                               "lineStartOffset": 0, "text": "line needle tail"}]


def test_random_boundary_property_never_excludes_actual_keyword_bytes():
    """随机分块与全字节基线比对；规划候选必须覆盖每个实际匹配涉及的文件。"""
    randomizer = random.Random(20260918)
    needle = b"needle"
    for _ in range(200):
        raw = bytearray(randomizer.choice(b"abc\n") for _ in range(randomizer.randrange(1, 80)))
        if randomizer.choice([True, False]):
            position = randomizer.randrange(len(raw) + 1)
            raw[position:position] = needle
        cuts = sorted(randomizer.sample(range(len(raw) + 1), randomizer.randrange(0, min(8, len(raw) + 1))))
        chunks = [bytes(raw)[left:right] for left, right in zip([0] + cuts, cuts + [len(raw)])]
        files, evidence = _files(chunks)
        selected = plan_candidate_files(files, evidence, "needle")
        start = 0
        while (start := bytes(raw).find(needle, start)) >= 0:
            end = start + len(needle)
            offset, involved = 0, set()
            for number, chunk in enumerate(chunks):
                if offset < end and offset + len(chunk) > start:
                    involved.add(f"file-{number}")
                offset += len(chunk)
            assert involved <= selected
            start += 1
