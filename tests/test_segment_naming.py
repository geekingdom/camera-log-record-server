"""日志片段命名使用真实服务器接收时间，保持安全名称和分卷唯一性。"""

from datetime import UTC, datetime

import pytest
from camera_logs.logs.naming import segment_filename, segment_name_from_source


def test_segment_name_uses_actual_shanghai_receive_seconds():
    assert segment_filename("自动化日志记录", "10.41.203.35", "2026-10-21T07:18:41.123+00:00",
                            "2026-10-21T07:24:08.999+00:00", 2) == (
        "自动化日志记录-10.41.203.35-20261021151841-20261021152408-part-000002.log")


def test_source_name_replacement_preserves_identity_and_same_second_part():
    source = "任务-含-横线-10.41.203.35-20261021150000-20261021160000-part-000002.log"
    instant = datetime(2026, 10, 21, 7, 20, 15, tzinfo=UTC)
    assert segment_name_from_source(source, instant, instant) == (
        "任务-含-横线-10.41.203.35-20261021152015-20261021152015-part-000002.log")


@pytest.mark.parametrize("first,last", [(None, None), ("bad", "bad"),
    ("2026-10-21T15:00:00", "2026-10-21T15:01:00")])
def test_missing_or_ambiguous_range_is_not_fabricated(first, last):
    assert segment_filename("task", "192.0.2.1", first, last, 3) == (
        "task-192.0.2.1-unknown-time-part-000003.log")


def test_names_are_safe_and_keep_range_under_filesystem_limit():
    name = segment_filename("../" + "测试" * 80, "2001:db8::1", "2026-10-21T15:10:00+08:00",
                            "2026-10-21T15:11:00+08:00", 1)
    assert len(name.encode()) <= 255
    assert "/" not in name and ":" not in name
    assert name.endswith("20261021151000-20261021151100-part-000001.log")


def test_legacy_source_and_clock_rollback_preserve_actual_endpoint_order():
    name = segment_name_from_source("part-000007.log", "2026-10-21T15:10:00+08:00",
                                    "2026-10-21T15:09:59+08:00")
    assert name.endswith("20261021151000-20261021150959-part-000007.log")
