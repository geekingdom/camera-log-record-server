"""配置设备 coredump 的 NFS 导出，不改写系统已有 exports。"""

import argparse
import ipaddress
import os
import re
import subprocess
import tempfile
from pathlib import Path

EXPORT_FILE = Path("/etc/exports.d/camera-logs-coredump.exports")
SAFE_ROOT = re.compile(r"/[A-Za-z0-9_./-]+")


def read_environment(path: Path) -> dict[str, str]:
    """按字面量读取 KEY=VALUE，避免部署配置被 shell 执行。"""
    values: dict[str, str] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not key or key in values:
            raise ValueError(f"部署配置第{number}行无效或键重复")
        values[key] = value
    return values


def nfs_settings(values: dict[str, str]) -> tuple[Path, str, str] | None:
    """校验启用条件；来源固定允许所有可达设备，旧网段字段不再参与导出。"""
    address = values.get("NFS_SERVER_IP", "").strip()
    if not address:
        return None
    root = Path(values.get("NFS_ROOT", ""))
    if not root.is_absolute() or root == Path("/") or ".." in root.parts or not SAFE_ROOT.fullmatch(str(root)):
        raise ValueError("NFS_ROOT 必须是非根绝对路径")
    try:
        parsed_address = ipaddress.ip_address(address)
    except ValueError as error:
        raise ValueError("NFS_SERVER_IP 必须是设备可达的IPv4或IPv6地址") from error
    if parsed_address.is_unspecified or parsed_address.is_loopback:
        raise ValueError("NFS_SERVER_IP 不能使用通配或回环地址")
    return root, str(parsed_address), "*"


def export_content(root: Path, owner: tuple[int, int] = (10001, 10001)) -> str:
    """生成允许任意可达设备写入且映射为 Worker UID/GID 的专属导出。"""
    return (
        "# 由 Camera Logs 部署器维护；不要在此文件添加其它业务导出。\n"
        f"{root} *(rw,sync,no_subtree_check,all_squash,anonuid={owner[0]},anongid={owner[1]})\n"
    )


def atomic_write(path: Path, content: str) -> None:
    """同目录原子替换专属配置，避免 exportfs 读取半写入内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def run(command: list[str]) -> None:
    """执行主机级安装或服务操作，命令中不包含设备凭据。"""
    subprocess.run(command, check=True)


def reject_symlink_ancestors(path: Path) -> None:
    """拒绝在已有符号链接下创建导出根，避免配置路径逃逸到非受管目录。"""
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        if current.exists() and current.is_symlink():
            raise ValueError("NFS_ROOT及其已有父目录不能是符号链接")


def configure(values: dict[str, str], *, export_file: Path = EXPORT_FILE, install_package: bool = False,
              owner: tuple[int, int] = (10001, 10001)) -> bool:
    """按配置创建根目录并刷新本项目导出；返回是否实际启用 NFS。"""
    settings = nfs_settings(values)
    if settings is None:
        # 仅撤销本项目专属导出；其它服务和nfs-server状态仍由其所有者管理。
        if export_file.exists():
            if os.geteuid() != 0:
                raise PermissionError("撤销 NFS 导出需要 root 权限")
            export_file.unlink()
            run(["exportfs", "-ra"])
        return False
    if os.geteuid() != 0:
        raise PermissionError("配置 NFS 导出需要 root 权限")
    root, _address, _network = settings
    if install_package:
        run(["apt-get", "update"])
        run(["apt-get", "install", "-y", "nfs-kernel-server"])
    reject_symlink_ancestors(root)
    root.mkdir(parents=True, exist_ok=True)
    # all_squash将设备写入映射至Worker UID/GID，保证下载端可读且不暴露设备root身份。
    os.chown(root, *owner)
    root.chmod(0o755)
    atomic_write(export_file, export_content(root, owner))
    run(["systemctl", "enable", "--now", "nfs-server"])
    run(["exportfs", "-ra"])
    return True


def main(argv: list[str] | None = None) -> int:
    """命令行入口供 Docker 部署脚本在 Compose 启动前调用。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--install-package", action="store_true")
    args = parser.parse_args(argv)
    configure(read_environment(args.env_file), install_package=args.install_package)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
