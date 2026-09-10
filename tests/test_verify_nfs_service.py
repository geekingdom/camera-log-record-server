"""NFS 实机验证脚本的单元测试：所有主机级命令均使用模拟。"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import verify_nfs_service as verify


def test_require_linux_root_rejects_non_linux(monkeypatch):
    """非 Linux 主机不得进入会修改 NFS 服务的实机流程。"""
    monkeypatch.setattr(verify.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(verify.os, "geteuid", lambda: 0)
    with pytest.raises(RuntimeError, match="Linux"):
        verify.require_linux_root()


def test_require_linux_root_rejects_non_root(monkeypatch):
    """Linux 非 root 账户不得创建导出或挂载。"""
    monkeypatch.setattr(verify.platform, "system", lambda: "Linux")
    monkeypatch.setattr(verify.os, "geteuid", lambda: 1000)
    with pytest.raises(PermissionError, match="root"):
        verify.require_linux_root()


@pytest.mark.parametrize(
    ("root", "export_file"),
    [
        (Path("/"), Path("/etc/exports.d/camera-logs-verify-safe.exports")),
        (Path("/tmp/camera-logs-nfs-verify-test"), Path("/etc/exports.d/camera-logs-verify-safe.exports")),
        (Path("/var/tmp/camera-logs-nfs-verify-test"), Path("/etc/exports")),
        (Path("/var/tmp/camera-logs-nfs-verify-test"), Path("/etc/exports.d/other.exports")),
    ],
)
def test_validate_paths_rejects_dangerous_verification_locations(root, export_file):
    """清理仅能作用于本脚本创建的临时根和专属 exports.d 文件。"""
    with pytest.raises(ValueError):
        verify.validate_paths(root, export_file)


def test_select_server_ip_filters_loopback_addresses(monkeypatch):
    """自动发现必须跳过 lo 与回环 IPv4，选择可由本机客户端访问的地址。"""
    response = json.dumps([
        {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1"}]},
        {"ifname": "eth0", "addr_info": [
            {"family": "inet", "local": "0.0.0.0"},
            {"family": "inet", "local": "192.0.2.44"},
        ]},
    ])
    monkeypatch.setattr(verify.subprocess, "check_output", lambda *_args, **_kwargs: response)
    assert verify.select_server_ip(None) == "192.0.2.44"
    assert verify.select_server_ip("198.51.100.8") == "198.51.100.8"


def test_parse_args_accepts_explicit_server_and_human_bytes():
    """CI 可覆盖文件大小与服务器地址，同时默认值保持 64 MiB。"""
    arguments = verify.parse_args(["--server-ip", "192.0.2.55", "--bytes", "2MiB"])
    assert arguments.server_ip == "192.0.2.55"
    assert arguments.bytes == 2 * 1024 * 1024
    assert verify.parse_args([]).bytes == 64 * 1024 * 1024


def test_cleanup_unmounts_before_removing_export_and_roots(tmp_path, monkeypatch):
    """正常收尾先卸载所有客户端，再刷新导出并删除本次临时目录。"""
    root = tmp_path / "camera-logs-nfs-verify-root"
    mount_a = tmp_path / "mount-a"
    mount_b = tmp_path / "mount-b"
    export_file = tmp_path / "camera-logs-verify-test.exports"
    state = verify.VerificationState(root, export_file, [
        verify.MountPoint(mount_a, 3, True), verify.MountPoint(mount_b, 4, True),
    ])
    calls = []
    monkeypatch.setattr(verify, "unmount", lambda path: calls.append(("unmount", path)) or True)
    monkeypatch.setattr(verify, "remove_export", lambda path: calls.append(("export", path)))
    monkeypatch.setattr(verify, "refresh_exports", lambda: calls.append(("refresh", None)))
    monkeypatch.setattr(verify, "remove_tree", lambda path: calls.append(("tree", path)))

    result = verify.cleanup(state)

    assert result["unmounted"] == {str(mount_a): True, str(mount_b): True}
    assert calls == [
        ("unmount", mount_a), ("unmount", mount_b), ("export", export_file), ("refresh", None),
        ("tree", root), ("tree", mount_a), ("tree", mount_b),
    ]


def test_cleanup_keeps_mount_directory_when_unmount_fails(tmp_path, monkeypatch):
    """卸载失败时不得递归删除该挂载点，以免删除服务器上的数据。"""
    root = tmp_path / "camera-logs-nfs-verify-root"
    mount = tmp_path / "mount-a"
    export_file = tmp_path / "camera-logs-verify-test.exports"
    state = verify.VerificationState(root, export_file, [verify.MountPoint(mount, 3, True)])
    removed = []
    monkeypatch.setattr(verify, "unmount", lambda _path: False)
    monkeypatch.setattr(verify, "remove_export", lambda _path: None)
    monkeypatch.setattr(verify, "refresh_exports", lambda: None)
    monkeypatch.setattr(verify, "remove_tree", lambda path: removed.append(path))

    result = verify.cleanup(state)

    assert result["unmounted"] == {str(mount): False}
    assert mount not in removed
    assert root not in removed


def test_cleanup_preserves_paths_when_failed_mount_command_left_a_mount(tmp_path, monkeypatch):
    """mount 超时后即使状态未登记，也要依据内核挂载状态保留所有相关路径。"""
    root = tmp_path / "camera-logs-nfs-verify-root"
    mount = tmp_path / "mount-a"
    state = verify.VerificationState(root, tmp_path / "camera-logs-verify-test.exports", [
        verify.MountPoint(mount, 3, False),
    ])
    removed = []
    monkeypatch.setattr(verify, "is_mounted", lambda _path: True)
    monkeypatch.setattr(verify, "unmount", lambda _path: False)
    monkeypatch.setattr(verify, "remove_export", lambda _path: None)
    monkeypatch.setattr(verify, "refresh_exports", lambda: None)
    monkeypatch.setattr(verify, "remove_tree", lambda path: removed.append(path) or True)

    result = verify.cleanup(state)

    assert result["unmounted"] == {str(mount): False}
    assert not result["rootRemoved"]
    assert removed == []


def test_cleanup_preserves_server_root_when_export_refresh_fails(tmp_path, monkeypatch):
    """导出删除或刷新失败时保留服务端根，防止仍被导出的路径数据丢失。"""
    root = tmp_path / "camera-logs-nfs-verify-root"
    mount = tmp_path / "mount-a"
    export_file = tmp_path / "camera-logs-verify-test.exports"
    export_file.write_text("keep", encoding="utf-8")
    state = verify.VerificationState(root, export_file, [verify.MountPoint(mount, 3, False)])
    removed = []
    monkeypatch.setattr(verify, "remove_export", lambda _path: (_ for _ in ()).throw(OSError("read-only")))
    monkeypatch.setattr(verify, "refresh_exports", lambda: (_ for _ in ()).throw(OSError("failed")))
    monkeypatch.setattr(verify, "remove_tree", lambda path: removed.append(path) or True)

    result = verify.cleanup(state)

    assert not result["rootRemoved"]
    assert str(root) in result["preservedPaths"]
    assert root not in removed
    assert mount in removed
