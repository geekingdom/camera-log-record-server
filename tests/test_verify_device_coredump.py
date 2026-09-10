"""设备 coredump 实机验证器的挂载与跨帧 PID 解析回归。"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_device_coredump as verify


def test_live_frames_reset_clears_text_buffer():
    """每条设备命令前重置文本缓冲，不调用字节数组专属方法。"""
    frames = verify.LiveFrames("ws://unused", "unused", "session-a")
    frames.data = "旧命令输出\n"
    frames.reset()
    assert frames.data == ""


def test_dsp_pid_accepts_server_prefixed_process_split_across_frames():
    """实时帧拼接且后续完整输出到达后，唯一目标进程可被解析。"""
    frames = verify.LiveFrames("ws://unused", "unused", "session-a")
    frames.data += "[2026-09-10 17:01:00] 321 root 0 S {Dsp_"
    frames.data += "Main} /home/hikdsp --service\n[2026-09-10 17:01:01] # \n"

    assert verify._dsp_pid(frames.data) == "321"


def test_dsp_pid_rejects_multiple_matching_processes():
    """两个完整匹配 PID 时绝不能猜测后续 kill 目标。"""
    output = (
        "[2026-09-10 17:01:00] 321 root {Dsp_Main} /home/hikdsp\n"
        "[2026-09-10 17:01:01] 322 root {Dsp_Main} /home/hikdsp\n"
        "[2026-09-10 17:01:02] # \n"
    )

    with pytest.raises(ValueError, match="多个"):
        verify._dsp_pid(output)


def test_dsp_pid_rejects_partial_line_until_later_output_confirms_ps_end():
    """末行尚未换行时不能把分包中的 PID 当作可 kill 的完整 ps 结果。"""
    partial = "[2026-09-10 17:01:00] 321 root {Dsp_Main} /home/hikdsp"

    assert verify._dsp_pid(partial) is None
    assert verify._dsp_pid(partial + "\n[2026-09-10 17:01:01] 普通设备日志\n") is None
    assert verify._dsp_pid(partial + "\n[2026-09-10 17:01:01] (ash)# \n") == "321"


def test_ps_echo_requires_exact_prefixed_command_line():
    """ps 输出内容不能冒充命令回显，必须先看到带时间前缀的精确 ps 行。"""
    assert verify._ps_echo("[2026-09-10 17:01:00] ps\n")
    assert verify._ps_echo("[2026-09-10 17:01:00] ps aux\n") is None


def test_after_uses_character_position_when_prior_output_contains_chinese():
    """正则返回的字符位置必须可直接用于截断，不能混用 UTF-8 字节下标。"""
    frames = verify.LiveFrames("ws://unused", "unused", "session-a")
    frames.data = "[2026-09-10 17:01:00] 中文前缀\n[2026-09-10 17:01:01] ps\n后续"
    echo = verify._ps_echo(frames.data)
    frames.after(echo.end())

    assert frames.data == "\n后续"


def test_mount_requires_exact_target_and_nfs_type():
    """只有带 type nfs 或 nfs4 的实际目标挂载行才能通过。"""
    target = "192.0.2.10:/srv/coredump/192.0.2.20"

    assert verify._mounted_nfs(f"[time] {target} on /mnt type nfs4 (rw)\n", target)
    assert verify._mounted_nfs(f"[time] {target} on /mnt type nfs (rw)\n", target)
    assert verify._mounted_nfs(f"[time] {target} on /mnt type ext4 (rw)\n", target) is None
    assert verify._mounted_nfs(f"[time] {target} on /mnt\n", target) is None


def test_observer_rejects_old_name_and_requires_changing_then_stable(monkeypatch):
    """旧名重登记不能冒充本次 core，目标 PID 必须经历持续观测。"""
    snapshots = iter((
        [{"id": "renewed-old", "name": "camera-core-6-Dsp_Main-654-1.gz", "size": 1, "sourceState": "STABLE"}],
        [{"id": "target", "name": "camera-core-6-Dsp_Main-654-2.gz", "size": 32, "sourceState": "CHANGING"}],
        [{"id": "target", "name": "camera-core-6-Dsp_Main-654-2.gz", "size": 64, "sourceState": "STABLE"}],
    ))

    async def listed(_client, _resource_id):
        return next(snapshots)

    monkeypatch.setattr(verify, "all_coredumps", listed)

    async def scenario():
        triggered = asyncio.Event()
        triggered.when = verify.time.monotonic()
        triggered.set()
        return await verify.observe_new_file(
            None, "resource", {"old"}, {"camera-core-6-Dsp_Main-654-1.gz"}, "654", triggered
        )

    found, _latency, history = asyncio.run(scenario())

    assert found["id"] == "target"
    assert [point["file"]["sourceState"] for point in history] == ["CHANGING", "STABLE"]


def test_observer_requires_changing_for_the_same_file_id(monkeypatch):
    """别的 core 已变化不能授权本次目标文件从稳定态直接通过。"""
    snapshots = iter((
        [{"id": "other", "name": "camera-core-6-Dsp_Main-654-1.gz", "size": 1, "sourceState": "CHANGING"}],
        [{"id": "target", "name": "camera-core-6-Dsp_Main-654-2.gz", "size": 32, "sourceState": "STABLE"}],
        [{"id": "target", "name": "camera-core-6-Dsp_Main-654-2.gz", "size": 64, "sourceState": "CHANGING"}],
        [{"id": "target", "name": "camera-core-6-Dsp_Main-654-2.gz", "size": 96, "sourceState": "STABLE"}],
    ))

    async def listed(_client, _resource_id):
        return next(snapshots)

    monkeypatch.setattr(verify, "all_coredumps", listed)

    async def scenario():
        triggered = asyncio.Event()
        triggered.when = verify.time.monotonic()
        triggered.set()
        return await verify.observe_new_file(None, "resource", set(), set(), "654", triggered)

    found, _latency, history = asyncio.run(scenario())

    assert found["id"] == "target"
    assert [point["file"]["sourceState"] for point in history if point["file"]["id"] == "target"] == ["STABLE", "CHANGING", "STABLE"]


@pytest.mark.parametrize("name", (
    "10.41.203.35-2127-1789058427.gz",
    "camera-core-6-Dsp_Main-2127.gz",
    "camera-core-6-Dsp_Main-21270-1.gz",
    "camera-core-6-Dsp_Main-2127x-1.gz",
))
def test_triggered_pid_filename_rejects_timestamp_ip_and_near_matches(name):
    """IP、时间戳及相邻 PID 不能因为包含数字而误认作本次核心文件。"""
    assert not verify._matches_triggered_pid(name, "2127")


def test_triggered_pid_filename_requires_exact_hikvision_core_pattern():
    """仅接受记录精确 PID 段的海康 core-6 Dsp_Main 文件名。"""
    assert verify._matches_triggered_pid("(none)/10.41.203.35-(none)-core-6-Dsp_Main-2127-1789058427.gz", "2127")


def test_all_coredumps_rejects_empty_page_before_total(monkeypatch):
    """服务端 total 不一致时空分页必须失败，不能在脚本里无限请求。"""
    async def listed(_client, _method, _path, **_kwargs):
        return {"items": [], "total": 1}

    monkeypatch.setattr(verify, "api", listed)

    with pytest.raises(RuntimeError, match="空页"):
        asyncio.run(verify.all_coredumps(None, "resource"))
