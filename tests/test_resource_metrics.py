"""验证资源级 CPU/内存监控的配置、租约和小时桶存储边界。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from camera_logs.resource_metrics.models import default_monitor_config, parse_monitor_config
from camera_logs.resource_metrics.store import ResourceMetricStore

pytest_plugins = ("test_resources",)


class _CaptureConnection:
    """命令捕获测试只模拟写端，设备输出仍手工交给唯一 observe_received 入口。"""

    def __init__(self):
        self.writes = []

    async def write(self, data):
        if getattr(self, "fail", False):
            self.fail = False
            raise OSError("write failed")
        self.writes.append(data)


from types import SimpleNamespace

from mongomock_motor import AsyncMongoMockClient


def test_default_monitor_config_has_expected_safe_metrics():
    """默认仅采集参考脚本中的通用内存和 CPU 指标。"""
    config = default_monitor_config()
    assert config["intervalSeconds"] == 60
    assert config["retentionDays"] == 90
    assert [(item["id"], item["unit"]) for item in config["items"]] == [
        ("mem-available", "KB"),
        ("slab", "KB"),
        ("cpu-idle", "%"),
    ]


def test_default_rules_match_reference_device_output():
    """默认规则可解析参考脚本关注的 BusyBox 输出形式。"""
    import regex

    config = parse_monitor_config(None)
    output = "MemAvailable:      123456 kB\nSlab: 7890 kB\nCpu(s):  1.0% us, 98.5% idle\n"
    values = [regex.search(item["pattern"], output, timeout=0.05).group(1) for item in config["items"]]
    assert values == ["123456", "7890", "98.5"]
    rules = {item["id"]: regex.compile(item["pattern"]) for item in config["processRules"]}
    assert rules["dsp-main"].search("{Dsp_Main} /home/hikdsp")
    assert rules["davinci"].search("davinci /usr/bin/davinci")
    assert rules["bll"].search("/heop/package/hello/fsa/hello 1+").group(1) == "hello"
    assert rules["ipp"].search("{ipp1_main} /heop/package/demo/dsp").group(1) == "demo"
    assert regex.search(config["processValuePattern"], "VmRSS:\t456 kB", timeout=0.05).group(1) == "456"


def test_regex_budget_does_not_include_device_io_but_rejects_slow_regex(monkeypatch):
    """网络命令耗时不由正则预算扣除，实际慢匹配仍必须被拒绝。"""
    import time

    from camera_logs.resource_metrics import runtime

    budget = [0.0]
    assert runtime._match(r"(1)", "1", budget).group(1) == "1"

    def slow(*_args, **_kwargs):
        time.sleep(0.41)

    monkeypatch.setattr(runtime.regex, "search", slow)
    with pytest.raises(runtime.ResourceMonitorUnavailable, match="REGEX_BUDGET_EXHAUSTED"):
        runtime._match(r"(1)", "1", [0.0])


def test_resource_monitor_lease_error_uses_stable_code():
    """租约丢失不依赖中文文案判断，跨部署语言环境仍可安全停止本轮。"""
    from camera_logs.resource_metrics.runtime import ResourceMonitorUnavailable

    assert ResourceMonitorUnavailable("RESOURCE_MONITOR_LEASE_LOST").code == "RESOURCE_MONITOR_LEASE_LOST"


def test_monitor_config_rejects_unsafe_command_and_invalid_process_template():
    """管理员配置不能借换行或错误 PID 占位符改变命令边界。"""
    with pytest.raises(ValueError, match="单行"):
        parse_monitor_config(
            {"items": [{"id": "x", "name": "x", "command": "a\nb", "pattern": "x", "unit": "KB"}]}
        )
    with pytest.raises(ValueError, match="{pid}"):
        parse_monitor_config({"processStatusCommand": "cat /proc/status"})


def test_monitor_config_rejects_invalid_regex_before_persisting():
    """无效规则在管理配置保存前失败，Worker 不会因坏正则崩溃。"""
    with pytest.raises(ValueError, match="正则"):
        parse_monitor_config(
            {"items": [{"id": "x", "name": "x", "command": "x", "pattern": "(", "unit": "KB"}]}
        )


@pytest.mark.asyncio
async def test_capture_waits_for_full_zero_exit_marker_and_cleans_failed_window():
    """只有唯一接收器喂入完整零退出标记后才交付监控输出，失败不遗留窗口。"""

    import re

    from camera_logs.collection.command_dispatcher import CommandCaptureError, CommandDispatcher

    connection = _CaptureConnection()
    dispatcher = CommandDispatcher(
        {},
        resolve_debug_password=None,
        on_debug=None,
        connection=lambda: connection,
        accepting=lambda: True,
        stopping=lambda: False,
        closed=lambda: False,
    )
    sender = asyncio.create_task(dispatcher.sender_loop())
    try:
        task = asyncio.create_task(dispatcher.capture_command("cat /proc/meminfo"))
        while not connection.writes:
            await asyncio.sleep(0)
        marker = re.search(rb"(__CAMERA_LOGS_METRIC_[0-9a-f]+__)", connection.writes[0]).group(1)
        dispatcher.observe_received(b"MemAvailable: 1 kB\n" + marker + b":")
        await asyncio.sleep(0)
        assert not task.done()
        dispatcher.observe_received(b"0\n")
        assert await task == b"MemAvailable: 1 kB"
        failed = asyncio.create_task(dispatcher.capture_command("false"))
        while len(connection.writes) < 2:
            await asyncio.sleep(0)
        failed_marker = re.search(rb"(__CAMERA_LOGS_METRIC_[0-9a-f]+__)", connection.writes[1]).group(1)
        dispatcher.observe_received(b"\n" + failed_marker + b":1\n")
        with pytest.raises(CommandCaptureError):
            await failed
        assert dispatcher._capture is None
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)


@pytest.mark.asyncio
async def test_capture_overflow_and_write_failure_do_not_block_later_command():
    """超限及写失败都会清理旁路窗口，后续同会话命令仍可取得响应。"""
    import re

    from camera_logs.collection.command_dispatcher import CommandCaptureError, CommandDispatcher

    connection = _CaptureConnection()
    dispatcher = CommandDispatcher(
        {},
        resolve_debug_password=None,
        on_debug=None,
        connection=lambda: connection,
        accepting=lambda: True,
        stopping=lambda: False,
        closed=lambda: False,
    )
    sender = asyncio.create_task(dispatcher.sender_loop())
    try:
        overflow = asyncio.create_task(dispatcher.capture_command("x", maximum=4))
        while not connection.writes:
            await asyncio.sleep(0)
        dispatcher.observe_received(b"0" * 256)
        with pytest.raises(CommandCaptureError):
            await overflow
        assert dispatcher._capture is None
        connection.fail = True
        with pytest.raises(OSError):
            await dispatcher.capture_command("x")
        assert dispatcher._capture is None
        later = asyncio.create_task(dispatcher.capture_command("x"))
        while len(connection.writes) < 2:
            await asyncio.sleep(0)
        marker = re.search(rb"(__CAMERA_LOGS_METRIC_[0-9a-f]+__)", connection.writes[-1]).group(1)
        dispatcher.observe_received(b"ok\n" + marker + b":0\r\n")
        assert await later == b"ok"
    finally:
        sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)


@pytest.mark.asyncio
async def test_hour_bucket_replaces_same_minute_without_extending_expiry():
    """同一分钟重试覆盖样本，固定小时 TTL 不因持续采样被延期。"""
    database = AsyncMongoMockClient().db
    store = ResourceMetricStore(database)
    stamp = datetime(2026, 9, 11, 3, 17, 42, tzinfo=UTC)
    first = {
        "sampledAt": stamp,
        "status": "OK",
        "values": [{"id": "slab", "name": "Slab", "value": 1, "unit": "KB"}],
        "configVersion": 2,
    }
    await store.save("resource-1", first, retention_days=90)
    await store.save(
        "resource-1",
        first | {"values": [{"id": "slab", "name": "Slab", "value": 2, "unit": "KB"}]},
        retention_days=30,
    )

    row = await database.resource_metric_hours.find_one({"resourceId": "resource-1"})
    assert len(row["samples"]) == 1
    assert row["samples"][0]["values"][0]["value"] == 2
    assert row["expiresAt"].replace(tzinfo=UTC) == datetime(2026, 9, 11, 3, tzinfo=UTC) + timedelta(days=90)


@pytest.mark.asyncio
async def test_history_cursor_is_time_ordered_and_never_returns_raw_capture():
    """历史 API 数据仅含结构化指标，游标按采样时间稳定续页。"""
    database = AsyncMongoMockClient().db
    store = ResourceMetricStore(database)
    start = datetime(2026, 9, 11, 1, tzinfo=UTC)
    for minute in range(3):
        await store.save(
            "resource-1",
            {
                "sampledAt": start + timedelta(minutes=minute),
                "status": "OK",
                "values": [],
                "configVersion": 1,
                "raw": "must-not-persist",
                "identity": {"model": "M", "subSerialNumber": "S"},
            },
            retention_days=90,
        )
    first = await store.history("resource-1", start, start + timedelta(hours=1), limit=2)
    assert [item["sampledAt"] for item in first["items"]] == [
        start + timedelta(minutes=2),
        start + timedelta(minutes=1),
    ]
    assert "raw" not in first["items"][0]
    assert first["items"][0]["identity"] == {"model": "M", "subSerialNumber": "S"}
    second = await store.history(
        "resource-1", start, start + timedelta(hours=1), limit=2, cursor=first["nextCursor"]
    )
    assert [item["sampledAt"] for item in second["items"]] == [start]


def test_resource_metric_history_route_limits_range_and_returns_public_config(resource_client):
    """只读用户沿用 tasks:read，接口不公开管理员命令或正则。"""
    repo = resource_client.app.state.repo
    stamp = datetime.now(UTC).replace(second=0, microsecond=0)
    resource_client.portal.call(
        repo.db.resources.insert_one,
        {
            "id": "camera",
            "kind": "HIKVISION_NETWORK",
            "healthStatus": "ONLINE",
            "deletedAt": None,
        },
    )

    async def save():
        await ResourceMetricStore(repo.db).save(
            "camera",
            {
                "sampledAt": stamp,
                "status": "OK",
                "values": [],
                "configVersion": 1,
            },
            retention_days=90,
        )

    resource_client.portal.call(save)
    response = resource_client.get(
        "/api/v1/resources/camera/resource-metrics",
        params={
            "start": (stamp - timedelta(minutes=1)).isoformat(),
            "end": (stamp + timedelta(minutes=1)).isoformat(),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"][0]["status"] == "OK"
    assert body["config"]["items"][0]["id"] == "mem-available"
    assert "command" not in body["config"]["items"][0]
    too_wide = resource_client.get(
        "/api/v1/resources/camera/resource-metrics",
        params={
            "start": (stamp - timedelta(days=32)).isoformat(),
            "end": stamp.isoformat(),
        },
    )
    assert too_wide.status_code == 422


def _runtime(database):
    return SimpleNamespace(
        repo=SimpleNamespace(db=database),
        task={"id": "t", "runId": "r", "resourceId": "x", "protocol": "SSH"},
        stopping=False,
        retired=False,
        collector=None,
    )


@pytest.mark.asyncio
async def test_sample_runtime_isolates_item_and_pid_failures(monkeypatch):
    """首项和一个 PID 失败时，其余指标及 PID 仍会进入同一结构化样本。"""
    from camera_logs.resource_metrics import runtime

    db = AsyncMongoMockClient().db
    rt = _runtime(db)
    collector = SimpleNamespace(_closed=asyncio.Event())
    rt.collector = collector
    await db.platform_settings.insert_one(
        {
            "id": "platform",
            "version": 1,
            "resourceMonitor": {**default_monitor_config(), "items": default_monitor_config()["items"][:2]},
        }
    )

    async def guard(*_):
        return {"model": "M", "subSerialNumber": "S"}

    calls = iter(
        [
            TimeoutError(),
            b"Slab: 7 kB",
            b"1 a 1 S {Dsp_Main} /home/hikdsp\n2 a 1 S {Dsp_Main} /home/hikdsp",
            TimeoutError(),
            b"VmRSS: 9 kB",
        ]
    )

    async def capture(*_a, **_k):
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value.decode()

    async def ash(**_):
        pass

    collector.ensure_ash_for_monitor = ash
    monkeypatch.setattr(runtime, "_guard", guard)
    monkeypatch.setattr(runtime, "_capture", capture)
    await runtime.sample_once(rt, collector)
    row = await db.resource_metric_hours.find_one({"resourceId": "x"})
    sample = row["samples"][0]
    assert (
        sample["status"] == "PARTIAL"
        and sample["values"][0]["id"] == "slab"
        and any(v.get("pid") == 2 for v in sample["values"])
    )


@pytest.mark.asyncio
async def test_sample_runtime_disabled_guard_never_writes(monkeypatch):
    """资源关闭监控时守卫直接拒绝，本轮不建立小时桶。"""
    from camera_logs.resource_metrics import runtime

    db = AsyncMongoMockClient().db
    rt = _runtime(db)
    collector = SimpleNamespace(_closed=asyncio.Event())
    rt.collector = collector

    async def guard(*_):
        return None

    monkeypatch.setattr(runtime, "_guard", guard)
    await runtime.sample_once(rt, collector)
    assert await db.resource_metric_hours.count_documents({}) == 0


@pytest.mark.asyncio
async def test_monitor_loop_recovers_after_one_failed_cycle(monkeypatch):
    """单轮异常被记录后，下一周期仍会重新采样。"""
    from camera_logs.resource_metrics import runtime

    rt = SimpleNamespace(stopping=False, retired=False, collector=None, task={"id": "t"})
    collector = SimpleNamespace(_closed=asyncio.Event())
    rt.collector = collector
    calls = []

    async def sample(*_):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError()
        rt.stopping = True

    async def sleep(_):
        return None

    monkeypatch.setattr(runtime, "sample_once", sample)
    monkeypatch.setattr(runtime.asyncio, "sleep", sleep)
    await runtime.monitor_loop(rt, collector)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_bad_process_rule_does_not_exhaust_other_rules(monkeypatch):
    """有问题的规则只报一次，其累计正则预算不能污染正常规则或后续PID。"""
    from camera_logs.resource_metrics import runtime

    db = AsyncMongoMockClient().db
    rt = _runtime(db)
    collector = SimpleNamespace(_closed=asyncio.Event())
    rt.collector = collector
    config = default_monitor_config()
    config["items"] = []
    config["processRules"] = [
        {"id": "broken", "name": "故障规则", "pattern": "bad", "enabled": True},
        {"id": "good", "name": "Dsp_Main", "pattern": "hikdsp", "enabled": True},
    ]
    await db.platform_settings.insert_one({"id": "platform", "resourceMonitor": config})

    async def guard(*_):
        return {"model": "M", "subSerialNumber": "S"}

    async def capture(_collector, command, _guard):
        return "1 a 1 S hikdsp\n2 a 1 S hikdsp" if command == "ps" else "VmRSS: 9 kB"

    async def ash(**_):
        return None

    collector.ensure_ash_for_monitor = ash
    original = runtime._match
    broken_calls = []

    def matched(pattern, output, budget):
        if pattern == "bad":
            broken_calls.append(1)
            budget[0] = 1
            raise runtime.ResourceMonitorUnavailable("REGEX_TIMEOUT")
        return original(pattern, output, budget)

    monkeypatch.setattr(runtime, "_guard", guard)
    monkeypatch.setattr(runtime, "_capture", capture)
    monkeypatch.setattr(runtime, "_match", matched)
    await runtime.sample_once(rt, collector)
    row = await db.resource_metric_hours.find_one({"resourceId": "x"})
    sample = row["samples"][0]
    assert sample["status"] == "PARTIAL"
    assert {value["pid"] for value in sample["values"]} == {1, 2}
    assert len(sample["errors"]) == len(broken_calls) == 1
