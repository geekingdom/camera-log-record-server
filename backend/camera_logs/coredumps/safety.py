"""NFS 接收目录和冻结副本的路径、文件类型安全检查。"""

from __future__ import annotations

import ipaddress
import os
import stat
from pathlib import Path


def nfs_root(settings) -> Path:
    """解析受控 NFS 根目录；根目录本身必须存在且不能是符号链接。"""
    root = Path(settings.nfs_root)
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("NFS_ROOT 必须是实际目录")
    return root.resolve(strict=True)


def device_directory(settings, ip: str) -> Path:
    """仅允许规范 IP 作为 NFS 一级目录名，拒绝穿越、别名与符号链接。"""
    normalized = str(ipaddress.ip_address(ip))
    root = nfs_root(settings)
    directory = root / normalized
    info = directory.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("设备 NFS 目录非法")
    resolved = directory.resolve(strict=True)
    if resolved.parent != root:
        raise ValueError("设备 NFS 目录越界")
    return resolved


def source_path(settings, ip: str, relative: str) -> Path:
    """从扫描产生的相对名称恢复路径，逐级拒绝符号链接和越界。"""
    directory = device_directory(settings, ip)
    relative_path = Path(relative)
    candidate = directory / relative_path
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("coredump 相对路径非法")
    current = directory
    for part in Path(relative).parts:
        if part in {"", ".", ".."}:
            raise ValueError("coredump 相对路径非法")
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("coredump 不允许符号链接")
    resolved = candidate.resolve(strict=True)
    if directory not in resolved.parents:
        raise ValueError("coredump 路径越界")
    return resolved


def open_source(settings, ip: str, relative: str) -> int:
    """以目录描述符逐层打开已扫描文件，避免检查路径后祖先被替换。"""
    relative_path = Path(relative)
    if relative_path.is_absolute() or ".." in relative_path.parts or not relative_path.parts:
        raise ValueError("coredump 相对路径非法")
    normalized = str(ipaddress.ip_address(ip))
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    root = os.open(nfs_root(settings), flags)
    descriptors = [root]
    try:
        current = os.open(normalized, flags, dir_fd=root)
        descriptors.append(current)
        for part in relative_path.parts[:-1]:
            if part in {"", ".", ".."}:
                raise ValueError("coredump 相对路径非法")
            current = os.open(part, flags, dir_fd=current)
            descriptors.append(current)
        final_part = relative_path.parts[-1]
        if final_part in {"", ".", ".."}:
            raise ValueError("coredump 相对路径非法")
        descriptor = os.open(final_part, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=current)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            os.close(descriptor)
            raise ValueError("coredump 必须是非硬链接普通文件")
        return descriptor
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def regular_unlinked_file(path: Path) -> os.stat_result:
    """只接收单链接普通文件，硬链接和特殊文件均不得成为下载源。"""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("coredump 必须是非硬链接普通文件")
    return info


def snapshots_root(settings) -> Path:
    """创建非 NFS 导出的 Worker 控制快照目录，设备写端无权修改。"""
    root = Path(settings.log_root).resolve().parent / "coredump-snapshots"
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("coredump 快照目录非法")
    resolved = root.resolve(strict=True)
    exported = nfs_root(settings)
    if resolved == exported or exported in resolved.parents:
        raise ValueError("coredump 快照目录不得位于 NFS 导出目录")
    return resolved
