"""流式搜索的读取边界、冻结水位及有限内存约束。"""

import io
from datetime import UTC, datetime

import pytest
from camera_logs.logs.search_stream import StreamSearch, index_entries

NOW = datetime(2026, 9, 9, tzinfo=UTC)
IDENTITY = {"taskId": "task", "runId": "run", "sessionId": "session", "nodeId": "node"}


def scan(scanner, data, number, **fields):
    """以带连续索引的冻结输入驱动匹配器，不使用真实设备。"""
    index = [{"offset": 0, "length": len(data), "sequence": number, "receivedAt": NOW.isoformat()}]
    return list(scanner.scan(io.BytesIO(data), index, IDENTITY | {"id": str(number)} | fields, lambda: False))


@pytest.mark.parametrize("split", range(1, 6))
def test_every_keyword_split_has_one_match_with_original_offset(split):
    scanner = StreamSearch(b"needle", NOW, NOW)
    assert not scan(scanner, b"prefix " + b"needle"[:split], 1)
    found = scan(scanner, b"needle"[split:] + b" suffix", 2)
    assert len(found) == 1 and found[0]["fileId"] == "1" and found[0]["offset"] == 7
    assert "needle" in found[0]["text"]
    assert len(scanner.tail) == len(scanner.origins) <= 5


@pytest.mark.parametrize("field", ["taskId", "runId", "sessionId", "nodeId"])
def test_changed_identity_does_not_join_tail(field):
    scanner = StreamSearch(b"needle", NOW, NOW)
    scan(scanner, b"nee", 1)
    assert not scan(scanner, b"dle", 2, **{field: "different"})


def test_frozen_partial_index_block_is_not_joined_to_next_file():
    scanner = StreamSearch(b"needle", NOW, NOW)
    index = [{"offset": 0, "length": 9, "sequence": 1, "receivedAt": NOW.isoformat()}]
    assert not list(scanner.scan(io.BytesIO(b"nee"), index, IDENTITY | {"id": "1"}, lambda: False))
    assert not scan(scanner, b"dle", 2)


def test_read_boundary_matches_once_and_single_byte_needle_never_duplicates():
    data = b"x" * (256 * 1024 - 3) + b"needle" + b"x" * 16
    scanner = StreamSearch(b"needle", NOW, NOW)
    found = scan(scanner, data, 1)
    assert [(item["fileId"], item["offset"]) for item in found] == [("1", 256 * 1024 - 3)]
    scanner = StreamSearch(b"n", NOW, NOW)
    assert len(scan(scanner, b"n", 1) + scan(scanner, b"n", 2)) == 2
    assert scanner.tail == b"" and scanner.origins == []


def test_index_is_consumed_lazily_before_first_result():
    """首个命中只需要当前与下一条索引，不能先加载百万条记录。"""
    consumed = []

    def entries():
        for number in range(1_000_000):
            consumed.append(number)
            yield {"offset": number, "length": 1, "sequence": number + 1, "receivedAt": NOW.isoformat()}

    scanner = StreamSearch(b"n", NOW, NOW)
    results = scanner.scan(io.BytesIO(b"n"), entries(), IDENTITY | {"id": "file"}, lambda: False)
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
