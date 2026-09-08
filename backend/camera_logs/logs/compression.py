"""独立进程完成小时压缩、双摘要校验和原子发布。

节点默认只有一个压缩进程，避免整点所有任务同时争抢 CPU。进程仅接收已关闭
文件的路径和清单，不能访问活动连接、数据库或写入器，失败时保留源文件。
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import multiprocessing
import os
import tarfile
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

_pool: ProcessPoolExecutor | None = None


def _hash_stream(stream):
    """分块计算摘要和字节数，校验正文与索引均未被截断或改变。"""
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def publish_archive(log: Path, index: Path, manifest: dict) -> Path:
    """在工作进程中发布归档；仅校验和同步全部成功后删除未压缩文件。"""
    final = log.with_suffix(".tar.gz")
    with index.open("rb") as source:
        manifest["indexSha256"], manifest["indexBytes"] = _hash_stream(source)
    fd, temporary = tempfile.mkstemp(prefix=final.name + ".", suffix=".partial", dir=final.parent)
    os.close(fd)
    partial = Path(temporary)
    try:
        with tarfile.open(partial, "w:gz", compresslevel=1) as bundle:
            bundle.add(log, arcname=log.name)
            bundle.add(index, arcname=index.name)
            encoded = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode()
            info = tarfile.TarInfo("manifest.json")
            info.size = len(encoded)
            bundle.addfile(info, io.BytesIO(encoded))
        with tarfile.open(partial, "r:gz") as bundle:
            for name, expected_hash, expected_size in (
                (log.name, manifest["sha256"], manifest["rawSize"]),
                (index.name, manifest["indexSha256"], manifest["indexBytes"]),
            ):
                stream = bundle.extractfile(name)
                if stream is None or _hash_stream(stream) != (expected_hash, expected_size):
                    raise OSError("归档正文或索引校验失败")
        with partial.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(partial, final)
        directory = os.open(final.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        log.unlink()
        index.unlink()
        return final
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


async def compress(log: Path, index: Path, manifest: dict) -> Path:
    """把压缩交给单独的 spawn 进程，不占用采集写入所使用的线程池。"""
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn"))
    return await asyncio.get_running_loop().run_in_executor(_pool, publish_archive, log, index, manifest)


async def shutdown_compression() -> None:
    """在节点连接与归档全部关闭后等待压缩进程退出，便于受控重新启动。"""
    global _pool
    if _pool is not None:
        pool, _pool = _pool, None
        await asyncio.to_thread(pool.shutdown, wait=True)
