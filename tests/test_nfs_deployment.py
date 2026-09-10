"""NFS coredump 部署配置回归：只模拟命令，不安装主机软件或修改 exports。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import configure_nfs_export as nfs
from deploy_env import create_environment
from native_config import defaults, validate


def enabled_values(tmp_path):
    """返回启用 NFS 的最小节点配置；客户端导出始终由部署器设为星号。"""
    return {
        "NFS_ROOT": str(tmp_path / "coredump"),
        "NFS_SERVER_IP": "192.0.2.30",
    }


def test_nfs_disabled_does_not_require_root_or_run_commands(tmp_path, monkeypatch):
    """默认留空时不能安装服务、创建目录或修改系统导出。"""
    monkeypatch.setattr(nfs.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(nfs, "run", lambda command: (_ for _ in ()).throw(AssertionError(command)))
    assert not nfs.configure({"NFS_ROOT": str(tmp_path / "unused"), "NFS_SERVER_IP": ""})


def test_nfs_export_is_atomic_unrestricted_and_replaces_legacy_cidr(tmp_path, monkeypatch):
    """导出固定为星号，重跑替换旧 CIDR 且不写入其它服务的 exports。"""
    commands = []
    ownership = []
    monkeypatch.setattr(nfs.os, "geteuid", lambda: 0)
    monkeypatch.setattr(nfs.os, "chown", lambda path, uid, gid: ownership.append((path, uid, gid)))
    monkeypatch.setattr(nfs, "run", lambda command: commands.append(command))
    values = enabled_values(tmp_path)
    export = tmp_path / "exports.d" / "camera-logs-coredump.exports"
    other = tmp_path / "exports.d" / "other-service.exports"
    export.parent.mkdir(parents=True)
    export.write_text("/legacy 192.0.2.0/24(rw)\n", encoding="utf-8")
    other.write_text("/other 198.51.100.0/24(rw)\n", encoding="utf-8")
    assert nfs.configure(values, export_file=export, install_package=True, owner=(123, 456))
    assert nfs.configure(values, export_file=export, owner=(123, 456))
    content = export.read_text(encoding="utf-8")
    assert f"{values['NFS_ROOT']} *(rw,sync,no_subtree_check,all_squash,anonuid=123,anongid=456)" in content
    assert "192.0.2.0/24" not in content
    assert other.read_text(encoding="utf-8") == "/other 198.51.100.0/24(rw)\n"
    assert commands[:2] == [["apt-get", "update"], ["apt-get", "install", "-y", "nfs-kernel-server"]]
    assert commands.count(["exportfs", "-ra"]) == 2
    assert ownership == [(tmp_path / "coredump", 123, 456), (tmp_path / "coredump", 123, 456)]


def test_nfs_disabled_removes_only_its_dedicated_export(tmp_path, monkeypatch):
    """关闭配置只删除项目专属导出并刷新，不停止nfs-server或触碰其它文件。"""
    commands = []
    export = tmp_path / "exports.d" / "camera-logs-coredump.exports"
    other = tmp_path / "exports.d" / "other-service.exports"
    export.parent.mkdir(parents=True)
    export.write_text("old", encoding="utf-8")
    other.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(nfs.os, "geteuid", lambda: 0)
    monkeypatch.setattr(nfs, "run", lambda command: commands.append(command))
    assert not nfs.configure({"NFS_SERVER_IP": ""}, export_file=export)
    assert not export.exists() and other.read_text(encoding="utf-8") == "keep"
    assert commands == [["exportfs", "-ra"]]


@pytest.mark.parametrize("values", [
    {"NFS_ROOT": "/", "NFS_SERVER_IP": "192.0.2.30"},
    {"NFS_ROOT": "/srv/coredump", "NFS_SERVER_IP": "127.0.0.1"},
    {"NFS_ROOT": "/srv/core dump", "NFS_SERVER_IP": "192.0.2.30"},
])
def test_nfs_settings_rejects_unsafe_roots_or_server_addresses(values):
    """星号导出不改变根目录与设备可达服务器地址的安全约束。"""
    with pytest.raises(ValueError):
        nfs.nfs_settings(values)


def test_nfs_rejects_existing_symlink_ancestor(tmp_path):
    """即使路径文本安全，已有符号链接也不能作为导出根的父目录。"""
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        nfs.reject_symlink_ancestors(link / "coredump")


def test_native_worker_uses_unrestricted_nfs_default_and_ignores_legacy_network(tmp_path):
    """原生配置不再要求或校验客户端 CIDR，旧键不会影响部署。"""
    values = defaults() | enabled_values(tmp_path)
    validate(values, "worker")
    validate(values | {"NFS_DEVICE_NETWORK": "2001:db8::/64"}, "worker")


def test_generated_environment_files_do_not_add_network_restriction(tmp_path):
    """新 Docker 配置不再生成会让升级后的节点继续受限的 CIDR 键。"""
    path = tmp_path / ".env"
    create_environment(path, "worker")
    assert "NFS_DEVICE_NETWORK=" not in path.read_text(encoding="utf-8")


def test_docker_worker_compose_files_preserve_same_nfs_bind_path():
    """完整和独立 Docker Worker 都把宿主和容器的 NFS_ROOT 绑定为相同绝对路径。"""
    root = Path(__file__).resolve().parents[1]
    for name in ("docker-compose.yml", "worker.yml", "worker-node.yml"):
        source = (root / "deploy" / name).read_text(encoding="utf-8")
        assert "NFS_ROOT: ${NFS_ROOT:-/srv/camera-logs/nfs-coredump}" in source
        assert "${NFS_ROOT:-/srv/camera-logs/nfs-coredump}:${NFS_ROOT:-/srv/camera-logs/nfs-coredump}" in source
        assert "NFS_SERVER_IP: ${NFS_SERVER_IP:-}" in source
        assert "NFS_DEVICE_NETWORK" not in source
