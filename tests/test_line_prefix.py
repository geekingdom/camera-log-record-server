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


def test_prefixer_preserves_csi_and_osc_sequences_split_across_three_chunks():
    """ANSI 控制序列可跨网络包，正文必须逐字节保留且不产生额外行前缀。"""
    prefixer = LinePrefixer()
    received_at = datetime(2026, 9, 8, 1, 2, 3, tzinfo=UTC)
    stamp = b"[2026-09-08 09:02:03] "
    chunks = (b"\x1b[3", b"1mrepeat\x1b]0;cam", b"era\x07\n\x1b[0mrepeat\n")

    result = b"".join(prefixer.prefix(chunk, received_at) for chunk in chunks)

    assert result == stamp + b"\x1b[31mrepeat\x1b]0;camera\x07\n" + stamp + b"\x1b[0mrepeat\n"
    assert result.count(stamp) == 2


def test_prefixer_preserves_repeated_crlf_when_carriage_return_and_lf_are_split():
    """CR 与 LF 分属不同包仍是一行；相同正文也必须保留为两个独立逻辑行。"""
    prefixer = LinePrefixer()
    received_at = datetime(2026, 9, 8, 1, 2, 3, tzinfo=UTC)
    stamp = b"[2026-09-08 09:02:03] "

    result = b"".join(prefixer.prefix(chunk, received_at) for chunk in (b"repeat\r", b"\nrepeat\r", b"\n"))

    assert result == stamp + b"repeat\r\n" + stamp + b"repeat\r\n"
    assert result.count(stamp) == 2
