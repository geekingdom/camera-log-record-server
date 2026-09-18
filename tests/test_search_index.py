"""旁路 FTS 候选索引只加速定位，不能保存正文或替代最终原文验证。"""

import shutil
import sqlite3
import tarfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from random import Random

from camera_logs.logs.search_index import build_index, index_path, query_index
from camera_logs.logs.search_plan import plan_candidate_files
from camera_logs.logs.search_stream import StreamSearch


def _archive(path, members):
    """写入最小共享小时包，正文仍只位于 tar.gz。"""
    with tarfile.open(path, "w:gz") as archive:
        for name, data in members.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, BytesIO(data))


def test_index_returns_chinese_candidate_across_decoder_chunk_without_storing_plaintext(tmp_path):
    """UTF-8 关键词跨读取块仍保守命中，SQLite 不含可展示的完整正文。"""
    archive = tmp_path / "hour.tar.gz"
    payload = b"prefix " + b"x" * (256 * 1024 - 8) + "企业检索".encode() + b" suffix\n"
    _archive(archive, {"part.log": payload})
    assert build_index(archive)
    found = query_index(archive, "企业检索")
    assert found and found["part.log"]["mayMatch"]
    assert found["part.log"]["bytes"] == len(payload)
    with sqlite3.connect(index_path(archive)) as database:
        dump = "\n".join(str(row) for row in database.execute("SELECT sql FROM sqlite_master"))
    assert "prefix" not in dump and "suffix" not in dump


def test_index_replacement_corruption_and_short_keyword_fall_back_to_unknown(tmp_path):
    """文件替换、损坏或不足三字符时不得信任候选索引。"""
    archive = tmp_path / "hour.tar.gz"
    _archive(archive, {"part.log": "企业检索\n".encode()})
    assert build_index(archive)
    assert query_index(archive, "企业检索")
    assert query_index(archive, "企业") is None
    _archive(archive, {"part.log": b"replacement\n"})
    assert query_index(archive, "企业检索") is None
    index_path(archive).write_bytes(b"not sqlite")
    assert query_index(archive, "replacement") is None


def test_index_keeps_candidate_after_body_nul_and_invalidates_hot_cache_on_replacement(tmp_path):
    """NUL 不能截断正文 FTS，已缓存的索引也不能越过归档或索引版本变化。"""
    archive = tmp_path / "hour.tar.gz"
    _archive(archive, {"part.log": b"prefix\x00needle suffix\n"})
    assert build_index(archive)
    first = query_index(archive, "needle")
    assert first and first["part.log"]["mayMatch"]
    # 已建立缓存后，归档身份变化仍须使 metadata 指纹校验失败。
    archive.touch()
    assert query_index(archive, "needle") is None

    replacement = tmp_path / "replacement.tar.gz"
    _archive(replacement, {"other.log": b"other content\n"})
    assert build_index(replacement)
    shutil.copyfile(index_path(replacement), index_path(archive))
    assert query_index(archive, "needle") is None


def test_index_candidates_never_lose_unicode_or_invalid_utf8_stream_matches(tmp_path):
    """FTS 词法差异只能增加候选，任意原始字节分卷后的字面检索结果必须不变。"""
    randomizer = Random(20260918)
    keywords = ["Needle", "café", "你好吗", "a b", "a-b", "🙂你好", "Ång", "x.y"]
    alphabet = "abXYZéÅ你好吗🙂 .-\"'"
    while len(keywords) < 40:
        keyword = "".join(randomizer.choice(alphabet) for _ in range(randomizer.randrange(3, 9)))
        if keyword not in keywords:
            keywords.append(keyword)
    members, files, contents = {}, [], {}
    sequence = 0
    for number, keyword in enumerate(keywords):
        needle = keyword.encode()
        # 先人为切入关键词内部，再随机切一次，覆盖多字节字符和非法字节相邻边界。
        raw = b"\xffprefix " + needle + b" suffix\n"
        inside = len(b"\xffprefix ") + randomizer.randrange(1, len(needle))
        second = randomizer.randrange(1, len(raw))
        cuts = sorted({inside, second})
        for part, payload in enumerate(raw[left:right] for left, right in zip([0] + cuts, cuts + [len(raw)])):
            name, identifier = f"case-{number}-{part}.log", f"case-{number}-{part}"
            members[name], contents[identifier] = payload, payload
            files.append({"id": identifier, "status": "READY", "bytes": len(payload), "archiveMember": name,
                          "taskId": "task", "runId": "run", "sessionId": "session", "nodeId": "node",
                          "sequence": sequence})
            sequence += 1
    archive = tmp_path / "unicode-boundaries.tar.gz"
    _archive(archive, members)
    assert build_index(archive)
    stamp = datetime(2026, 9, 18, tzinfo=UTC)

    def scan(selected, keyword):
        scanner, results = StreamSearch(keyword.encode(), stamp - timedelta(seconds=1), stamp + timedelta(seconds=1)), []
        for item in selected:
            payload = contents[item["id"]]
            results.extend(scanner.scan(BytesIO(payload), [{"offset": 0, "length": len(payload),
                "receivedAt": stamp.isoformat(), "sequence": item["sequence"]}], item, lambda: False))
        results.extend(scanner.finish())
        return results

    for keyword in keywords:
        evidence = query_index(archive, keyword)
        assert evidence is not None
        selected = plan_candidate_files(files, {
            item["id"]: evidence[item["archiveMember"]] for item in files
        }, keyword)
        subset = [item for item in files if item["id"] in selected]
        assert scan(subset, keyword) == scan(files, keyword)
