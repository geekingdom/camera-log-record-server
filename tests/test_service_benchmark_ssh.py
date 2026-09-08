"""本地 SSH 压测源需校验密码、保留正文并回收底层加密连接。"""

import asyncio
import hashlib
import importlib
from pathlib import Path

import asyncssh
import httpx
import pytest


def module(monkeypatch):
    """载入与命令行一致的工具模块路径。"""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("service_benchmark_ssh")


async def test_ssh_source_authenticates_and_preserves_bytes(monkeypatch):
    source = module(monkeypatch).SshLoadSource(1, "127.0.0.1", 15, 64, password="synthetic")
    await source.start()
    try:
        with pytest.raises(asyncssh.PermissionDenied):
            await asyncssh.connect("127.0.0.1", port=source.port, username="benchmark", password="wrong", known_hosts=None)
        async with asyncssh.connect("127.0.0.1", port=source.port, username="benchmark", password="synthetic", known_hosts=None) as connection:
            process = await connection.create_process(term_type="xterm", encoding=None)
            await asyncio.wait_for(source.connected.wait(), 2)
            source.release.set()
            await source.emit(1)
            received = await asyncio.wait_for(process.stdout.readexactly(source.source_bytes), 2)
            assert hashlib.sha256(received).hexdigest() == source.source_sha256
            assert source.connection_count == 1
        await asyncio.wait_for(source.transport_closed.wait(), 2)
    finally:
        await source.close()
    assert not source.connections


async def test_device_info_requires_temporary_credentials(monkeypatch):
    device = module(monkeypatch).DeviceInfoSource("127.0.0.1", "synthetic", port=0)
    await device.start()
    try:
        async with httpx.AsyncClient() as client:
            url = f"http://127.0.0.1:{device.port}/ISAPI/System/deviceInfo"
            assert (await client.get(url)).status_code == 401
            response = await client.get(url, auth=("benchmark", "synthetic"))
            assert response.status_code == 200
            assert "<model>SSH-BENCHMARK</model>" in response.text
    finally:
        await device.close()
