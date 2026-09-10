"""在隔离 Linux root CI 中实际配置、挂载并校验一次独立的 NFS 导出。

正式配置会启用并启动 nfs-server；为保护其它共享，本脚本完成后不停止该服务。
"""

import argparse
import concurrent.futures
import contextlib
import hashlib
import ipaddress
import json
import os
import platform
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from configure_nfs_export import configure

TEMPORARY_PARENT = Path("/var/tmp")
EXPORTS_PARENT = Path("/etc/exports.d")
ROOT_PREFIX = "camera-logs-nfs-verify-"
EXPORT_PREFIX = "camera-logs-verify-"
DEFAULT_BYTES = 64 * 1024 * 1024
BLOCK_BYTES = 1024 * 1024
COMMAND_TIMEOUT_SECONDS = 60


@dataclass
class MountPoint:
    """记录单个客户端挂载点及其挂载状态，清理时据此防止误删远端数据。"""

    path: Path
    version: int
    mounted: bool = False


@dataclass
class VerificationState:
    """本次验证专属资源；不会引用或清理已有 NFS 导出、服务和目录。"""

    root: Path
    export_file: Path
    mounts: list[MountPoint]


class VerificationError(RuntimeError):
    """保留主体失败与收尾结果，使 CI 即使失败也能上传可操作的诊断。"""

    def __init__(self, cause: Exception, cleanup_result: dict[str, object]):
        super().__init__(str(cause))
        self.cleanup_result = cleanup_result


def require_linux_root() -> None:
    """限制实机流程只在 Linux root 下执行，保护开发机和非特权调用。"""
    if platform.system() != "Linux":
        raise RuntimeError("NFS 实机验证只能在 Linux 主机运行")
    if os.geteuid() != 0:
        raise PermissionError("NFS 实机验证需要 root 权限")


def validate_paths(root: Path, export_file: Path) -> None:
    """限定可删除资源的命名空间，避免异常收尾影响任意主机路径。"""
    if (not root.is_absolute() or root == Path("/") or root.parent != TEMPORARY_PARENT
            or not root.name.startswith(ROOT_PREFIX)):
        raise ValueError("NFS 临时根必须是 /var/tmp 下的本次专属目录")
    if (export_file.parent != EXPORTS_PARENT or not export_file.name.startswith(EXPORT_PREFIX)
            or export_file.suffix != ".exports"):
        raise ValueError("NFS 导出文件必须是 /etc/exports.d 下的本次专属文件")


def parse_bytes(value: str) -> int:
    """解析 CI 可读的字节参数，保持写入大小精确且拒绝空值和负数。"""
    text = value.strip().upper()
    units = (("GIB", 1024 * 1024 * 1024), ("MIB", 1024 * 1024), ("KIB", 1024), ("B", 1))
    for suffix, multiplier in units:
        if text.endswith(suffix):
            number = text[:-len(suffix)]
            break
    else:
        number, multiplier = text, 1
    try:
        parsed = int(number)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--bytes 必须是正整数或如 64MiB") from error
    result = parsed * multiplier
    if result <= 0:
        raise argparse.ArgumentTypeError("--bytes 必须大于零")
    return result


def select_server_ip(explicit: str | None) -> str:
    """优先使用显式地址，否则从 ip JSON 中选择首个非回环 IPv4 地址。"""
    if explicit:
        try:
            address = ipaddress.ip_address(explicit)
        except ValueError as error:
            raise ValueError("--server-ip 必须是 IPv4 地址") from error
        if address.version != 4 or address.is_loopback or address.is_unspecified or address.is_link_local:
            raise ValueError("--server-ip 必须是可达的非回环 IPv4 地址")
        return str(address)
    output = subprocess.check_output(["ip", "-j", "address", "show"], text=True,
                                     timeout=COMMAND_TIMEOUT_SECONDS)
    for interface in json.loads(output):
        for item in interface.get("addr_info", []):
            if item.get("family") != "inet":
                continue
            try:
                address = ipaddress.ip_address(item.get("local", ""))
            except ValueError:
                continue
            if not address.is_loopback and not address.is_unspecified and not address.is_link_local:
                return str(address)
    raise RuntimeError("未从 ip -j 找到可用于本机 NFS 挂载的非回环 IPv4 地址")


def run(command: list[str], *, timeout: int = COMMAND_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    """有界执行主机命令并捕获输出，确保验证 stdout 始终保持 JSONL 格式。"""
    return subprocess.run(command, check=True, text=True, capture_output=True, timeout=timeout)


def refresh_exports() -> None:
    """仅重新加载 exports.d 配置，不撤销其它导出也不改变 NFS 服务状态。"""
    run(["exportfs", "-ra"])


def remove_export(path: Path) -> None:
    """删除本次专属导出文件，随后由调用方刷新导出表。"""
    path.unlink(missing_ok=True)


def remove_tree(path: Path) -> bool:
    """删除已确认不是挂载点的临时目录，并按实际文件系统状态返回结果。"""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return True
    return not path.exists()


def unmount(path: Path) -> bool:
    """尝试卸载单个客户端目录；失败时留给人工诊断且禁止后续递归删除。"""
    try:
        run(["umount", str(path)], timeout=COMMAND_TIMEOUT_SECONDS)
    except (subprocess.SubprocessError, OSError):
        return False
    return True


def is_mounted(path: Path) -> bool:
    """查询内核实际挂载状态，处理 mount 命令报错前已成功挂载的边界。"""
    return os.path.ismount(path)


def cleanup(state: VerificationState) -> dict[str, object]:
    """按挂载、导出、根目录的顺序收尾，隔离失败的挂载点免遭 rmtree。"""
    unmounted: dict[str, bool] = {}
    errors: list[str] = []
    preserved_paths: list[str] = []
    for mount in state.mounts:
        try:
            actual_mounted = is_mounted(mount.path)
        except OSError as error:
            actual_mounted = True
            errors.append(f"读取客户端挂载状态失败: {error}")
        if mount.mounted or actual_mounted:
            command_succeeded = unmount(mount.path)
            try:
                actual_mounted = is_mounted(mount.path)
            except OSError as error:
                actual_mounted = True
                errors.append(f"读取卸载后挂载状态失败: {error}")
            mount.mounted = not command_succeeded or actual_mounted
            unmounted[str(mount.path)] = not mount.mounted
    try:
        remove_export(state.export_file)
    except OSError as error:
        errors.append(f"删除导出文件失败: {error}")
    export_removed = not state.export_file.exists()
    exports_refreshed = True
    try:
        refresh_exports()
    except (subprocess.SubprocessError, OSError) as error:
        exports_refreshed = False
        errors.append(f"刷新导出失败: {error}")
    # 未成功撤销并刷新导出，或任一客户端仍挂载时，宿主根仍可能被访问，必须完整保留。
    root_removed = export_removed and exports_refreshed and not any(mount.mounted for mount in state.mounts)
    if root_removed:
        try:
            root_removed = remove_tree(state.root)
        except OSError as error:
            root_removed = False
            errors.append(f"删除导出根失败: {error}")
    if not root_removed:
        preserved_paths.append(str(state.root))
    for mount in state.mounts:
        if not mount.mounted:
            try:
                if not remove_tree(mount.path):
                    errors.append(f"删除客户端目录失败: {mount.path}")
                    preserved_paths.append(str(mount.path))
            except OSError as error:
                errors.append(f"删除客户端目录失败: {error}")
                preserved_paths.append(str(mount.path))
        else:
            preserved_paths.append(str(mount.path))
    return {"unmounted": unmounted, "exportRemoved": export_removed, "exportsRefreshed": exports_refreshed,
            "rootRemoved": root_removed, "preservedPaths": preserved_paths, "errors": errors}


def mount_export(server_ip: str, root: Path, mount: MountPoint) -> None:
    """用指定 NFS 协议版本挂载本机导出，成功后立即登记清理责任。"""
    run(["mount", "-t", "nfs", "-o", f"vers={mount.version}", f"{server_ip}:{root}", str(mount.path)])
    mount.mounted = True


def verify_mount_protocol(mount: MountPoint) -> None:
    """通过 findmnt 的 JSON 结果确认内核实际挂载为请求的 NFS 协议版本。"""
    completed = run(["findmnt", "--json", "--target", str(mount.path), "--output", "FSTYPE,OPTIONS"])
    filesystems = json.loads(completed.stdout).get("filesystems", [])
    if not filesystems:
        raise RuntimeError(f"findmnt 未找到挂载点: {mount.path}")
    details = filesystems[0]
    options = str(details.get("options", ""))
    if details.get("fstype") not in {"nfs", "nfs4"} or f"vers={mount.version}" not in options:
        raise RuntimeError(f"挂载协议未满足 NFSv{mount.version}: {details}")


@contextlib.contextmanager
def bounded_configure_commands():
    """让正式配置函数复用本脚本的超时和输出捕获规则，结束后恢复原实现。"""
    import configure_nfs_export as nfs_export

    original_run = nfs_export.run
    nfs_export.run = run
    try:
        yield
    finally:
        nfs_export.run = original_run


def configure_export(root: Path, server_ip: str, export_file: Path) -> None:
    """调用正式配置入口后确认服务已启用、运行且本次专属导出确为星号规则。"""
    with bounded_configure_commands():
        configure({"NFS_ROOT": str(root), "NFS_SERVER_IP": server_ip}, export_file=export_file)
    run(["systemctl", "is-enabled", "--quiet", "nfs-server"])
    run(["systemctl", "is-active", "--quiet", "nfs-server"])
    content = export_file.read_text(encoding="utf-8")
    if f"{root} *(" not in content:
        raise RuntimeError("本次专属 NFS 导出未包含星号来源规则")


def sha256_file(path: Path) -> str:
    """流式计算文件摘要，避免验证阶段把大文件再次整体读入内存。"""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def write_payload(path: Path, size_bytes: int, device_ip: str) -> str:
    """以设备专属固定块流式写入并 fsync，返回客户端写入侧摘要。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = hashlib.sha256(device_ip.encode("ascii")).digest()
    block = (seed * ((BLOCK_BYTES + len(seed) - 1) // len(seed)))[:BLOCK_BYTES]
    digest = hashlib.sha256()
    remaining = size_bytes
    with path.open("wb", buffering=0) as output:
        while remaining:
            chunk = block[:min(len(block), remaining)]
            written = output.write(chunk)
            if written != len(chunk):
                raise OSError(f"NFS 写入不完整: {written}/{len(chunk)}")
            digest.update(chunk)
            remaining -= len(chunk)
        output.flush()
        os.fsync(output.fileno())
    return digest.hexdigest()


def verify_payload(mount: MountPoint, root: Path, device_ip: str, size_bytes: int) -> dict[str, object]:
    """对客户端写入、宿主路径和客户端回读三处逐项比较内容、长度及所有权。"""
    client_path = mount.path / device_ip / "payload.bin"
    server_path = root / device_ip / "payload.bin"
    client_digest = write_payload(client_path, size_bytes, device_ip)
    server_stat = server_path.stat()
    server_digest = sha256_file(server_path)
    read_digest = sha256_file(client_path)
    if (server_stat.st_size != size_bytes or server_stat.st_uid != 10001 or server_stat.st_gid != 10001
            or len({client_digest, server_digest, read_digest}) != 1):
        raise RuntimeError(f"NFS 内容、长度或 UID/GID 校验失败: {device_ip}")
    return {"deviceIp": device_ip, "bytes": size_bytes, "uid": server_stat.st_uid, "gid": server_stat.st_gid,
            "sha256": client_digest, "nfsVersion": mount.version}


def execute(server_ip: str, size_bytes: int) -> dict[str, object]:
    """完成一次双协议、双设备写入和重复配置后的真实 NFS 验证。"""
    suffix = uuid.uuid4().hex
    root = Path(tempfile.mkdtemp(prefix=ROOT_PREFIX, dir=TEMPORARY_PARENT))
    mounts = [
        MountPoint(Path(tempfile.mkdtemp(prefix=f"{ROOT_PREFIX}mount-v3-", dir=TEMPORARY_PARENT)), 3),
        MountPoint(Path(tempfile.mkdtemp(prefix=f"{ROOT_PREFIX}mount-v4-", dir=TEMPORARY_PARENT)), 4),
    ]
    state = VerificationState(root, EXPORTS_PARENT / f"{EXPORT_PREFIX}{suffix}.exports", mounts)
    validate_paths(root, state.export_file)
    result: dict[str, object] = {"serverIp": server_ip, "bytesPerDevice": size_bytes,
                                 "nfsVersions": [3, 4], "cleanup": None}
    failure: Exception | None = None
    try:
        configure_export(root, server_ip, state.export_file)
        for mount in mounts:
            mount_export(server_ip, root, mount)
            verify_mount_protocol(mount)
        devices = [(mounts[0], "192.0.2.101"), (mounts[1], "192.0.2.102")]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            payloads = list(pool.map(lambda pair: verify_payload(pair[0], root, pair[1], size_bytes), devices))
        for mount in mounts:
            if not unmount(mount.path):
                raise RuntimeError(f"无法卸载重配前客户端目录: {mount.path}")
            try:
                mount.mounted = is_mounted(mount.path)
            except OSError as error:
                mount.mounted = True
                raise RuntimeError(f"无法确认重配前客户端已卸载: {mount.path}") from error
            if mount.mounted:
                raise RuntimeError(f"重配前客户端目录仍处于挂载状态: {mount.path}")
        configure_export(root, server_ip, state.export_file)
        mount_export(server_ip, root, mounts[0])
        verify_mount_protocol(mounts[0])
        if sha256_file(mounts[0].path / "192.0.2.101" / "payload.bin") != payloads[0]["sha256"]:
            raise RuntimeError("重复配置后的 NFSv3 挂载读取摘要不一致")
        result.update({"passed": True, "payloads": payloads, "reconfigureMount": "NFSv3"})
    except Exception as error:  # noqa: BLE001 - 必须在 finally 后携带清理状态报告原始失败。
        failure = error
    finally:
        result["cleanup"] = cleanup(state)
    if failure is not None:
        raise VerificationError(failure, result["cleanup"])
    if result["cleanup"]["errors"] or not result["cleanup"]["rootRemoved"]:
        raise VerificationError(RuntimeError("NFS 验证完成但临时资源未完全清理"), result["cleanup"])
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析 CI 参数；默认完整执行 64 MiB x 两路的验证负载。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-ip", help="本机可达的 NFS 服务器 IPv4；默认由 ip -j 自动选择")
    parser.add_argument("--bytes", type=parse_bytes, default=DEFAULT_BYTES, help="每个设备写入字节数，默认 64MiB")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """命令行入口输出单行 JSON 结果；失败也保留错误类型供 CI 诊断。"""
    arguments = parse_args(argv)
    try:
        require_linux_root()
        result = execute(select_server_ip(arguments.server_ip), arguments.bytes)
    except Exception as error:  # noqa: BLE001 - 实机校验必须把任何失败写入 CI 证据。
        report: dict[str, object] = {"passed": False, "errorType": type(error).__name__, "message": str(error)}
        if isinstance(error, VerificationError):
            report["cleanup"] = error.cleanup_result
        print(json.dumps(report, ensure_ascii=False), flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
