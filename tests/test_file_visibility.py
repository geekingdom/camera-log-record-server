"""独立文件探针必须保留慢样本并拒绝不完整或乱序的可见性证据。"""

import asyncio
import json
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from benchmark_file_visibility import (
    VisibilityAggregator,
    _event_batch,
    _read_events,
    _tail_logs,
    execute,
    line,
    parse_visible_lines,
)

PREFIX = b"[2026-09-20 12:00:00] "


def test_parser_waits_for_complete_line_and_preserves_body():
    raw = line(0, 0, 256)
    stored = PREFIX + raw
    records, pending = parse_visible_lines(stored[:41])
    assert records == []
    records, pending = parse_visible_lines(pending + stored[41:])
    assert records == [(0, 0, raw)]
    assert pending == b""


def test_parser_rejects_missing_server_timestamp():
    with pytest.raises(AssertionError):
        parse_visible_lines(line(0, 0, 256))


def test_visible_event_can_arrive_before_receive_event():
    raw = line(0, 0, 256)
    tracker = VisibilityAggregator({(0, 0): raw})
    tracker.visible(0, 0, raw, 10.125)
    assert not tracker.complete
    tracker.received(0, 0, 10.0)
    assert tracker.complete
    report = tracker.report()
    assert report["samples"] == 1
    assert report["maxMs"] == pytest.approx(125)


def test_missing_sample_cannot_produce_successful_report():
    tracker = VisibilityAggregator({(0, 0): line(0, 0, 256)})
    tracker.received(0, 0, 10)
    with pytest.raises(AssertionError):
        tracker.report()


@pytest.mark.parametrize("kind", ["received", "visible"])
def test_duplicate_events_are_rejected(kind):
    raw = line(0, 0, 256)
    tracker = VisibilityAggregator({(0, 0): raw})
    if kind == "received":
        tracker.received(0, 0, 10)
        with pytest.raises(AssertionError):
            tracker.received(0, 0, 10.1)
    else:
        tracker.visible(0, 0, raw, 10.1)
        with pytest.raises(AssertionError):
            tracker.visible(0, 0, raw, 10.2)


def test_negative_latency_is_not_clamped_to_success():
    raw = line(0, 0, 256)
    tracker = VisibilityAggregator({(0, 0): raw})
    tracker.received(0, 0, 10)
    with pytest.raises(AssertionError):
        tracker.visible(0, 0, raw, 9)


def test_reader_rejects_corrupt_body_and_unknown_route():
    raw = line(0, 0, 256)
    for route, value in [(0, raw.replace(b"x", b"y", 1)), (1, raw)]:
        tracker = VisibilityAggregator({(0, 0): raw})
        with pytest.raises(AssertionError):
            tracker.visible(route, 0, value, 10)


def test_each_route_is_ordered_independently():
    expected = {(route, seq): line(route, seq, 256) for route in range(2) for seq in range(2)}
    tracker = VisibilityAggregator(expected)
    for route, seq in [(1, 0), (0, 0), (0, 1), (1, 1)]:
        tracker.received(route, seq, 10)
        tracker.visible(route, seq, expected[route, seq], 10.01)
    assert tracker.complete
    assert tracker.report()["samples"] == 4


def test_reader_must_not_sort_away_out_of_order_lines():
    expected = {(0, seq): line(0, seq, 256) for seq in range(2)}
    tracker = VisibilityAggregator(expected)
    with pytest.raises(AssertionError):
        tracker.visible(0, 1, expected[0, 1], 10)


@pytest.mark.parametrize("at", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_clock_values_are_rejected(at):
    raw = line(0, 0, 256)
    tracker = VisibilityAggregator({(0, 0): raw})
    with pytest.raises(AssertionError):
        tracker.received(0, 0, at)
    with pytest.raises(AssertionError):
        tracker.visible(0, 0, raw, at)


def test_real_spawn_reader_collects_every_sample_and_cleans_logs(tmp_path):
    command = [sys.executable, "scripts/benchmark_file_visibility.py", "--routes", "2",
               "--seconds", "1", "--lines-per-second", "20", "--work-root", str(tmp_path)]
    result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, timeout=30, check=True)
    report = json.loads(result.stdout)
    assert report["samples"] == 40
    assert report["filePageCacheReadable"] is True
    assert report["fsyncMeasured"] is False
    assert list(tmp_path.iterdir()) == []


async def test_cancelled_probe_removes_only_its_own_temporary_directory(tmp_path):
    protected = tmp_path / "unrelated.log"
    protected.write_bytes(b"must remain")
    options = SimpleNamespace(routes=1, seconds=10, lines_per_second=20, line_bytes=128,
                              timeout_seconds=10, ipc_queue=64, work_root=str(tmp_path))
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(.05):
            await execute(options)
    assert list(tmp_path.iterdir()) == [protected]
    assert protected.read_bytes() == b"must remain"


def test_reader_joins_timestamp_and_body_split_across_ordered_parts(tmp_path):
    root = tmp_path / "visibility-route-0000-probe"
    root.mkdir()
    raw = line(0, 0, 256) + line(0, 1, 256)
    stored = PREFIX + raw[:256] + PREFIX + raw[256:]
    for index, part in enumerate([stored[:9], stored[9:333], stored[333:]]):
        (root / f"part-{index:06d}.log").write_bytes(part)
    events = queue.Queue()
    stop = threading.Event()
    stop.set()
    _tail_logs(str(tmp_path), events, stop)
    first, second, ready, done = (events.get_nowait() for _ in range(4))
    assert first[1:4] == (0, 0, raw[:256])
    assert second[1:4] == (0, 1, raw[256:])
    assert ready == ("ready",)
    assert done == ("done",)


def test_reader_final_scan_rejects_trailing_partial_line(tmp_path):
    root = tmp_path / "visibility-route-0000-probe"
    root.mkdir()
    (root / "part-000001.log").write_bytes(PREFIX + line(0, 0, 256) + PREFIX + b"unfinished")
    events = queue.Queue()
    stop = threading.Event()
    stop.set()
    with pytest.raises(AssertionError, match="incomplete"):
        _tail_logs(str(tmp_path), events, stop)
    assert events.get_nowait()[0] == "visible"
    assert events.get_nowait()[0] == "ready"
    assert events.get_nowait()[0] == "error"


def test_reader_final_scan_exposes_extra_complete_line(tmp_path):
    root = tmp_path / "visibility-route-0000-probe"
    root.mkdir()
    raw = line(0, 0, 256)
    (root / "part-000001.log").write_bytes(PREFIX + raw + PREFIX + line(0, 1, 256))
    events = queue.Queue()
    stop = threading.Event()
    stop.set()
    _tail_logs(str(tmp_path), events, stop)
    tracker = VisibilityAggregator({(0, 0): raw})
    tracker.received(0, 0, 0)
    _, route, sequence, body, at = events.get_nowait()
    tracker.visible(route, sequence, body, at)
    _, route, sequence, body, at = events.get_nowait()
    with pytest.raises(AssertionError):
        tracker.visible(route, sequence, body, at)


def test_ipc_batch_has_a_hard_limit_and_keeps_order():
    events = queue.Queue()
    for index in range(300):
        events.put((index,))
    result = _event_batch(events, .1)
    assert result == [(index,) for index in range(256)]
    assert events.get_nowait() == (256,)


async def test_full_batch_is_checked_even_after_last_expected_sample():
    raw = line(0, 0, 256)
    tracker = VisibilityAggregator({(0, 0): raw})
    tracker.received(0, 0, 1)
    events = queue.Queue()
    events.put(("visible", 0, 0, raw, 2))
    events.put(("visible", 0, 1, line(0, 1, 256), 2))
    with pytest.raises(AssertionError):
        await _read_events(events, tracker, time.monotonic() + 1, SimpleNamespace(is_alive=lambda: True))
