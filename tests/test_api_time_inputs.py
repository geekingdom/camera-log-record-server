"""验证 HTTP 时间入参统一按上海墙钟时间解释。"""

from datetime import UTC, datetime

import pytest
from camera_logs.administration.api import _utc_range
from camera_logs.common.api_time import parse_api_datetime
from camera_logs.resource_metrics.api import _time

pytest_plugins = ("test_api",)


@pytest.mark.parametrize(("value", "expected"), [
    ("2026-10-19T00:30:00", datetime(2026, 10, 18, 16, 30, tzinfo=UTC)),
    ("2026-10-19T00:30:00+08:00", datetime(2026, 10, 18, 16, 30, tzinfo=UTC)),
    ("2026-10-18T16:30:00Z", datetime(2026, 10, 18, 16, 30, tzinfo=UTC)),
])
def test_api_datetime_accepts_shanghai_wall_clock_and_explicit_offsets(value, expected):
    """无时区输入跨 UTC 日期时仍必须定位到与 +08:00 相同的绝对时刻。"""
    assert parse_api_datetime(value) == expected


@pytest.mark.parametrize("value", [
    "", "2026-10-19", "2026-10-19T25:00:00", "not-a-time", "9999-12-31T23:59:59-12:00",
])
def test_api_datetime_rejects_invalid_datetime(value):
    """日期、空值和越界时间不能静默变成查询边界。"""
    with pytest.raises(ValueError):
        parse_api_datetime(value)


def test_admin_and_metric_ranges_accept_shanghai_wall_clock():
    """管理查询和设备指标查询复用相同的无时区解释规则。"""
    interval = _utc_range("2026-10-19T00:00:00", "2026-10-19T01:00:00")
    assert interval == {
        "$gte": datetime(2026, 10, 18, 16, tzinfo=UTC),
        "$lt": datetime(2026, 10, 18, 17, tzinfo=UTC),
    }
    assert _time("2026-10-19T00:00:00", datetime.now(UTC)) == interval["$gte"]


def test_coredump_received_range_accepts_shanghai_wall_clock_and_rejects_invalid_time(client):
    """Coredump 首次接收筛选支持无时区输入，并保留非法输入的 422 边界。"""
    repo = client.app.state.repo
    client.portal.call(repo.db.coredump_files.insert_many, [
        {"id": "core-before", "resourceId": "fixture-device", "nodeId": "test", "name": "before.core",
         "receivedAt": datetime(2026, 10, 18, 15, 59, tzinfo=UTC)},
        {"id": "core-inside", "resourceId": "fixture-device", "nodeId": "test", "name": "inside.core",
         "receivedAt": datetime(2026, 10, 18, 16, 30, tzinfo=UTC)},
    ])
    response = client.get("/api/v1/resources/fixture-device/coredumps", params={
        "receivedFrom": "2026-10-19T00:00:00", "receivedTo": "2026-10-19T01:00:00",
    })
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == ["core-inside"]
    invalid = client.get("/api/v1/resources/fixture-device/coredumps", params={"receivedFrom": "invalid"})
    assert invalid.status_code == 422
