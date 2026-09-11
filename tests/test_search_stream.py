"""流式搜索的读取边界、冻结水位及有限内存约束。"""

import io
from datetime import UTC, datetime, timedelta

import pytest
from camera_logs.logs.search_stream import MAX_RESULT_TEXT_BYTES, StreamSearch, index_entries

NOW = datetime(2026, 9, 9, tzinfo=UTC)
RANGE_END = NOW + timedelta(seconds=1)
IDENTITY = {"taskId": "task", "runId": "run", "sessionId": "session", "nodeId": "node"}


def scan(scanner, data, number, **fields):
    """以带连续索引的冻结输入驱动匹配器，不使用真实设备。"""
    index = [{"offset": 0, "length": len(data), "sequence": number, "receivedAt": NOW.isoformat()}]
    return list(scanner.scan(io.BytesIO(data), index, IDENTITY | {"id": str(number)} | fields, lambda: False))


@pytest.mark.parametrize("split", range(1, 6))
def test_every_keyword_split_has_one_match_with_original_offset(split):
    scanner = StreamSearch(b"needle", NOW, RANGE_END)
    assert not scan(scanner, b"prefix " + b"needle"[:split], 1)
    assert not scan(scanner, b"needle"[split:] + b" suffix", 2)
    found = list(scanner.finish())
    assert len(found) == 1 and found[0]["fileId"] == "1" and found[0]["offset"] == 7
    assert "needle" in found[0]["text"]
    assert len(scanner.tail) == len(scanner.origins) <= 5


@pytest.mark.parametrize("field", ["taskId", "runId", "sessionId", "nodeId"])
def test_changed_identity_does_not_join_tail(field):
    scanner = StreamSearch(b"needle", NOW, RANGE_END)
    scan(scanner, b"nee", 1)
    assert not scan(scanner, b"dle", 2, **{field: "different"})


def test_frozen_partial_index_block_is_not_joined_to_next_file():
    scanner = StreamSearch(b"needle", NOW, RANGE_END)
    index = [{"offset": 0, "length": 9, "sequence": 1, "receivedAt": NOW.isoformat()}]
    assert not list(scanner.scan(io.BytesIO(b"nee"), index, IDENTITY | {"id": "1"}, lambda: False))
    assert not scan(scanner, b"dle", 2)


def test_read_boundary_matches_once_and_single_byte_needle_never_duplicates():
    data = b"x" * (256 * 1024 - 100) + b"needle" + b"x" * 16 + b"\n"
    scanner = StreamSearch(b"needle", NOW, RANGE_END)
    found = scan(scanner, data, 1)
    assert [(item["fileId"], item["offset"]) for item in found] == [("1", 256 * 1024 - 100)]
    scanner = StreamSearch(b"n", NOW, RANGE_END)
    assert len(scan(scanner, b"n\n", 1) + scan(scanner, b"n\n", 2)) == 2
    assert scanner.tail == b"" and scanner.origins == []


def test_match_returns_one_complete_line_without_neighbouring_lines():
    scanner = StreamSearch(b"needle", NOW, RANGE_END)
    found = scan(scanner, b"before\nfirst needle and needle again\nafter\n", 1)
    assert found == [{"fileId": "1", "offset": 13, "lineStartFileId": "1", "lineStartOffset": 7,
                      "text": "first needle and needle again"}]


def test_match_uses_first_keyword_within_time_range_when_line_has_earlier_match():
    scanner = StreamSearch(b"needle", NOW, RANGE_END)
    data = b"needle before range needle in range\n"
    index = [
        {"offset": 0, "length": 20, "sequence": 1, "receivedAt": NOW.replace(year=2025).isoformat()},
        {"offset": 20, "length": len(data) - 20, "sequence": 2, "receivedAt": NOW.isoformat()},
    ]
    found = list(scanner.scan(io.BytesIO(data), index, IDENTITY | {"id": "range"}, lambda: False))
    assert len(found) == 1 and found[0]["offset"] == 20


def test_crlf_and_non_utf8_line_are_explicitly_presented():
    scanner = StreamSearch(b"needle", NOW, RANGE_END)
    found = scan(scanner, b"needle \xff\r\n", 1)
    assert found[0]["text"] == "needle �"
    assert found[0]["encodingError"] is True


def test_match_split_across_read_boundary_returns_complete_utf8_line():
    prefix = b"x" * (256 * 1024 - 100)
    scanner = StreamSearch(b"needle", NOW, RANGE_END)
    found = scan(scanner, prefix + b"needle \xe6\x91\x84\xe5\x83\x8f\xe6\x9c\xba\nnext\n", 1)
    assert len(found) == 1
    assert found[0]["offset"] == len(prefix)
    assert found[0]["text"] == ("x" * len(prefix)) + "needle 摄像机"


def test_last_line_without_newline_is_returned_by_finish():
    scanner = StreamSearch(b"needle", NOW, RANGE_END)
    assert scan(scanner, b"last needle line", 1) == []
    assert list(scanner.finish()) == [{"fileId": "1", "offset": 5, "lineStartFileId": "1", "lineStartOffset": 0,
                                       "text": "last needle line"}]


def test_oversized_matching_line_fails_instead_of_claiming_a_truncated_line():
    scanner = StreamSearch(b"needle", NOW, RANGE_END)
    data = b"x" * MAX_RESULT_TEXT_BYTES + b"needle" + b"y\n"
    with pytest.raises(ValueError, match="超过"):
        scan(scanner, data, 1)


def test_keyword_time_range_includes_start_and_excludes_exact_end():
    """关键词搜索与原始时间查询共用半开区间 [start, end) 语义。"""
    start, end = NOW, RANGE_END
    included = StreamSearch(b"needle", start, end)
    included_index = [{"offset": 0, "length": 7, "sequence": 1, "receivedAt": start.isoformat()}]
    assert list(included.scan(io.BytesIO(b"needle\n"), included_index, IDENTITY | {"id": "start"}, lambda: False))
    excluded = StreamSearch(b"needle", start, end)
    excluded_index = [{"offset": 0, "length": 7, "sequence": 1, "receivedAt": end.isoformat()}]
    assert list(excluded.scan(io.BytesIO(b"needle\n"), excluded_index, IDENTITY | {"id": "end"}, lambda: False)) == []


def test_index_is_consumed_lazily_before_first_result():
    """首个命中只需要当前与下一条索引，不能先加载百万条记录。"""
    consumed = []

    def entries():
        for number in range(1_000_000):
            consumed.append(number)
            yield {"offset": number, "length": 1, "sequence": number + 1, "receivedAt": NOW.isoformat()}

    scanner = StreamSearch(b"n", NOW, RANGE_END)
    results = scanner.scan(io.BytesIO(b"n\n"), entries(), IDENTITY | {"id": "file"}, lambda: False)
    assert next(results)["offset"] == 0
    assert len(consumed) <= 2
    results.close()


def test_index_reader_limits_each_read_and_rejects_oversized_records():
    class Reader(io.BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 65536
            return super().read(size)

    assert list(index_entries(Reader(b'{"offset":0}\n{"offset":1}'), lambda: False)) == [
        {"offset": 0}, {"offset": 1}]
    with pytest.raises(ValueError, match="过长"):
        list(index_entries(Reader(b"x" * 16385), lambda: False))
