"""完整行检索的正文预算必须在线程收集阶段生效，而非写数据库前才截断。"""

from pathlib import Path

from camera_logs.logs import jobs


def test_text_budget_stops_source_before_unbounded_collection(monkeypatch):
    """多字节正文按 UTF-8 字节计费，超预算的整行不返回且不继续消费源。"""
    consumed = []

    def scan(*_args):
        for index in range(1000):
            consumed.append(index)
            yield {"fileId": "file", "offset": index * 6, "text": "中文"}

    monkeypatch.setattr(jobs, "_scan_archive", scan)
    results, size, truncated = jobs._search_limited(Path("unused"), None, {}, 1000, 12, lambda: False)
    assert [item["offset"] for item in results] == [0, 6]
    assert consumed == [0, 1, 2]
    assert size == 12 and truncated is True


def test_final_unterminated_line_obeys_remaining_budget():
    """末尾无换行行不能绕过之前各分卷已消耗的正文预算。"""
    class Scanner:
        def finish(self):
            yield {"fileId": "file", "offset": 0, "text": "中文"}

    assert jobs._finish_search_limited(Scanner(), 10, 5) == ([], 0, True)
    rows, size, truncated = jobs._finish_search_limited(Scanner(), 10, 6)
    assert rows[0]["text"] == "中文" and size == 6 and truncated is False
