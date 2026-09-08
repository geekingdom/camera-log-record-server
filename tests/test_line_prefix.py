"""日志行前缀测试：验证分包、CRLF、空行和相同正文不会破坏时序。"""

from datetime import UTC, datetime

from camera_logs.collection.line_prefix import LinePrefixer


def test_prefixer_adds_one_prefix_per_logical_line_across_chunks():
    prefixer = LinePrefixer()
    stamp = datetime(2026, 9, 8, 1, 2, 3, tzinfo=UTC)
    result = prefixer.prefix(b"partial", stamp) + prefixer.prefix(b"\r\n\nrepeat\nrepeat\n", stamp)
    assert result.count(b"[") == 4
    assert result.endswith(b"] repeat\n[2026-09-08 09:02:03] repeat\n")


def test_prefixer_handles_long_unterminated_line_without_extra_prefix():
    prefixer = LinePrefixer()
    stamp = datetime(2026, 9, 8, 1, 2, 3, tzinfo=UTC)
    first = prefixer.prefix(b"x" * 1_000_000, stamp)
    second = prefixer.prefix(b"tail\n", stamp)
    assert first.count(b"[") == 1
    assert second.count(b"[") == 0
    assert (first + second).endswith(b"x" * 10 + b"tail\n")
