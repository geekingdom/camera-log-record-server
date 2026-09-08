"""用户多小时下载的有界 ZIP STORE 输出，包含 ZIP64 和目录尾部计费。"""

import zipfile
from pathlib import Path

from camera_logs.logs.archive_access import LimitedWriter, read_limiter


def write_zip(destination: Path, hourly: list[Path], limit: int) -> int:
    """顺序打包现有小时压缩文件，达到产物上限前拒绝后续字节写入。"""
    with destination.open("wb") as target:
        output = LimitedWriter(target, limit, "export output exceeds size limit")
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as bundle:
            for path in hourly:
                # 流式写入前未知成员终值，主动启用 ZIP64 以支持超过 2 GB 的小时包。
                with path.open("rb") as source, bundle.open(path.name, "w", force_zip64=True) as entry:
                    while data := source.read(1024 * 1024):
                        read_limiter.consume(len(data))
                        entry.write(data)
    return destination.stat().st_size
