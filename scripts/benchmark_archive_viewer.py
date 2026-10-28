"""比较真实gzip归档首屏与重叠翻页的解压工作量，所有模拟文件随临时目录回收。"""

import gzip
import io
import json
import tarfile
import tempfile
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from camera_logs.logs.archive_readers import ArchiveReaders
from camera_logs.logs.source import open_log_source


def main():
    """用64MiB归档比较旧目录扫描与新有界读取；不代表生产磁盘或NFS容量。"""
    size = 64 * 1024 * 1024
    block = (b'[2026-10-28 12:00:00] synthetic archive viewer benchmark\n' * 4096)
    data = (block * (size // len(block) + 1))[:size]
    with tempfile.TemporaryDirectory(prefix='archive-viewer-benchmark-') as directory:
        path = Path(directory) / 'hour.tar.gz'
        with tarfile.open(path, 'w:gz', compresslevel=1) as archive:
            member = tarfile.TarInfo('part.log')
            member.size = size
            archive.addfile(member, io.BytesIO(data))
        original = gzip._GzipReader.read
        decompressed = [0]

        def measure(reader, amount=-1):
            result = original(reader, amount)
            decompressed[0] += len(result)
            return result

        with patch.object(gzip._GzipReader, 'read', measure):
            started = perf_counter()
            with open_log_source(path, 'part.log') as source:
                assert source.stream.read(262144) == data[:262144]
            baseline = {'ms': round((perf_counter() - started) * 1000, 3), 'decompressedBytes': decompressed[0]}
            decompressed[0] = 0
            pool = ArchiveReaders()
            try:
                started = perf_counter()
                assert pool.read('file', path, 'part.log', 0, 262144, size) == data[:262144]
                first = {'ms': round((perf_counter() - started) * 1000, 3), 'decompressedBytes': decompressed[0]}
                decompressed[0] = 0
                started = perf_counter()
                for offset in (261888, 523904, 261888, 523904, 785920):
                    assert pool.read('file', path, 'part.log', offset, 262144, size) == data[offset:offset + 262144]
                paging = {'ms': round((perf_counter() - started) * 1000, 3), 'decompressedBytes': decompressed[0]}
                assert first['decompressedBytes'] < 1024 * 1024
                assert paging['decompressedBytes'] < 1024 * 1024
            finally:
                pool.close()
        print(json.dumps({'passed': True, 'rawBytes': size, 'baselineFirst': baseline,
                          'optimizedFirst': first, 'fiveNavigationReads': paging,
                          'temporaryFilesRemovedOnExit': True}))


if __name__ == '__main__':
    main()
