"""服务压测输入输出工具回归：真实本地 Telnet 与流式归档边界必须可靠。"""

import asyncio
import hashlib
import io
import runpy
import tarfile
import zipfile
from pathlib import Path

import pytest

tools = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/service_benchmark_io.py"))
LoadSource = tools["LoadSource"]
verify_download = tools["verify_download"]
PREFIX = b"[2026-09-08 13:59:59] "


def package(parts):
    """建立按指定成员顺序的小时 tar.gz，允许测试构造半行和异常成员。"""
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, raw in parts:
            member = tarfile.TarInfo(name)
            member.size = len(raw)
            archive.addfile(member, io.BytesIO(raw))
    return output.getvalue()


def stored_download(path, entries):
    """写入 ZIP STORE 下载包，外层入口可故意乱序以验证小时自然排序。"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as output:
        for name, raw in entries:
            output.writestr(name, raw)


def source_line(route, sequence):
    """构造与压测源协议一致的最小原始正文。"""
    return f"route={route:04d} seq={sequence:09d} payload\n".encode()


def prefixed(route, sequence):
    """为原始正文补上服务端时间前缀。"""
    return PREFIX + source_line(route, sequence)


def test_load_source_real_telnet_releases_and_closes():
    """真实本地 Telnet 连接在 release 后精确发送，完成后仍可由 close 回收。"""
    async def scenario():
        source = LoadSource(7, "127.0.0.1", 15, 64)
        await source.start()
        reader, writer = await tools["telnetlib3"].open_connection(host="127.0.0.1", port=source.port, encoding=False)
        await asyncio.wait_for(source.connected.wait(), 2)
        source.release.set()
        await source.emit(1)
        received = b""
        while len(received) < source.source_bytes:
            received += await asyncio.wait_for(reader.read(source.source_bytes - len(received)), 2)
        assert hashlib.sha256(received).hexdigest() == source.source_sha256
        assert source.source_lines == 15 and source.connection_count == 1 and source.failure is None
        with pytest.raises(ValueError):
            await source.emit(0)
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(source.peer_closed.wait(), 2)
        await source.close()
    asyncio.run(scenario())


def test_verify_download_rejoins_half_lines_and_orders_zip_hours(tmp_path):
    """ZIP 内小时包乱序时按名称排序，跨 part 的时间前缀和半行仍应正确重组。"""
    path = tmp_path / "download.zip"
    first, second = prefixed(1, 0), prefixed(1, 1)
    stored_download(path, [
        ("20260908140000.tar.gz", package([("part-000001.log", second)])),
        ("20260908130000.tar.gz", package([("part-000001.log", first[:12]), ("part-000002.log", first[12:])])),
    ])
    expected = source_line(1, 0) + source_line(1, 1)
    result = verify_download(path, hashlib.sha256(expected).hexdigest(), 2)
    assert result["routeLines"] == {1: 2}


def test_verify_download_rejects_truncated_final_part(tmp_path):
    """最后一个分卷不以换行结束时，不能把半行当作完整日志忽略。"""
    path = tmp_path / "truncated.tar.gz"
    path.write_bytes(package([("part-000001.log", prefixed(1, 0)[:-1])]))
    with pytest.raises(AssertionError):
        verify_download(path, hashlib.sha256(source_line(1, 0)).hexdigest(), 1)


def test_verify_download_bounds_unterminated_damaged_line(tmp_path):
    """损坏归档持续输出无换行正文时，校验器必须在单行合同上限处拒绝。"""
    path = tmp_path / "oversized-pending.tar.gz"
    path.write_bytes(package([("part-000001.log", b"x" * (tools["MAX_PENDING_BYTES"] + 1))]))
    with pytest.raises(AssertionError, match="单行上限"):
        verify_download(path, hashlib.sha256(b"").hexdigest(), 0)


def test_verify_download_keeps_half_line_across_utc_ordered_files(tmp_path):
    """主线程按 UTC 小时传入多个下载文件时，半行必须跨文件连续重组。"""
    earlier, later = tmp_path / "08.tar.gz", tmp_path / "09.tar.gz"
    raw = prefixed(1, 0)
    earlier.write_bytes(package([("part-000001.log", raw[:15])]))
    later.write_bytes(package([("part-000001.log", raw[15:])]))
    result = verify_download([earlier, later], hashlib.sha256(source_line(1, 0)).hexdigest(), 1)
    assert result["storedBytes"] == len(raw) and result["logParts"] == 2


@pytest.mark.parametrize("parts", [
    [("part-000001.log", prefixed(1, 0)), ("part-000001.log", prefixed(1, 1))],
    [("part-000001.log", prefixed(1, 0)), ("part-000003.log", prefixed(1, 1))],
    [("part-000001.log", prefixed(1, 0)), ("part-000002.log", prefixed(1, 0))],
    [("part-000001.log", prefixed(1, 0)), ("part-000002.log", prefixed(2, 1))],
    [("part-000001.log", prefixed(1, 0)), ("attachment.txt", b"not a log")],
])
def test_verify_download_rejects_duplicate_missing_polluted_and_non_log(tmp_path, parts):
    """重复、缺失、跨路序号污染和附件不能被压测归档验收误判为成功。"""
    path = tmp_path / "download.tar.gz"
    path.write_bytes(package(parts))
    expected = hashlib.sha256(source_line(1, 0) + source_line(1, 1)).hexdigest()
    with pytest.raises(AssertionError):
        verify_download(path, expected, 2)
