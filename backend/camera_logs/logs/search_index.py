"""小时归档的 SQLite FTS5 旁路候选索引，不保存可还原的日志正文。"""

from __future__ import annotations

import codecs
import os
import shutil
import sqlite3
import tarfile
import tempfile
import threading
from collections import OrderedDict
from pathlib import Path

SCHEMA_VERSION = 2
MAX_INDEX_BYTES = 256 * 1024 * 1024
MIN_FREE_BYTES = 128 * 1024 * 1024
PAGE_SIZE = 4096
CHUNK_CHARS = 32 * 1024
_verified: OrderedDict[tuple[str, str], bool] = OrderedDict()
_verification_lock = threading.Lock()


def _healthy(connection, path: Path) -> bool:
    """不可变索引每个文件版本只做一次全库检查；替换或修改后重新检查。"""
    key = (str(path.resolve()), _fingerprint(path))
    with _verification_lock:
        if key in _verified:
            _verified.move_to_end(key)
            return True
    if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
        return False
    if _fingerprint(path) != key[1]:
        return False
    with _verification_lock:
        _verified[key] = True
        while len(_verified) > 64:
            _verified.popitem(last=False)
    return True


def index_path(archive_path: Path) -> Path:
    """索引与不可变小时包同目录存放，绝不加入用户下载 tar.gz。"""
    return Path(str(archive_path) + ".search.sqlite")


def _fingerprint(path: Path) -> str:
    """绑定已发布归档 inode、大小和修改时间，替换文件后的候选必定失效。"""
    value = path.stat()
    return _stat_fingerprint(value)


def _stat_fingerprint(value) -> str:
    """从已打开文件或路径状态生成同一格式的归档身份。"""
    return f"{value.st_dev}:{value.st_ino}:{value.st_size}:{value.st_mtime_ns}"


def _supported(connection) -> bool:
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(value, tokenize='trigram', content='', detail=none)")
        connection.execute("DROP TABLE probe")
        return True
    except sqlite3.OperationalError:
        return False


def _schema(connection, fingerprint: str) -> bool:
    connection.execute(f"PRAGMA page_size={PAGE_SIZE}")
    connection.execute(f"PRAGMA max_page_count={MAX_INDEX_BYTES // PAGE_SIZE}")
    if not _supported(connection):
        return False
    connection.executescript("""
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE members (name TEXT PRIMARY KEY, bytes INTEGER NOT NULL, head BLOB NOT NULL,
            tail BLOB NOT NULL, has_newline INTEGER NOT NULL);
        CREATE TABLE chunks (rowid INTEGER PRIMARY KEY, member TEXT NOT NULL REFERENCES members(name));
        CREATE VIRTUAL TABLE terms USING fts5(value, tokenize='trigram', content='', detail=none);
    """)
    connection.executemany("INSERT INTO metadata(key, value) VALUES (?, ?)", [
        ("schemaVersion", str(SCHEMA_VERSION)), ("archiveFingerprint", fingerprint), ("complete", "true"),
    ])
    return True


def build_index(archive_path: Path) -> bool:
    """流式建立并原子发布索引；失败仅返回 false，不能阻止日志压缩发布。"""
    archive_path = Path(archive_path)
    destination = index_path(archive_path)
    try:
        if shutil.disk_usage(archive_path.parent).free < MAX_INDEX_BYTES + MIN_FREE_BYTES:
            return False
        descriptor, temporary_name = tempfile.mkstemp(prefix=destination.name + ".", suffix=".partial", dir=destination.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            connection = sqlite3.connect(temporary)
            try:
                rowid = 0
                with archive_path.open("rb") as archive_file:
                    fingerprint = _stat_fingerprint(os.fstat(archive_file.fileno()))
                    if _fingerprint(archive_path) != fingerprint:
                        return False
                    if not _schema(connection, fingerprint):
                        return False
                    with tarfile.open(fileobj=archive_file, mode="r|gz") as archive:
                        for member in archive:
                            if shutil.disk_usage(archive_path.parent).free < MIN_FREE_BYTES:
                                raise OSError("索引构建磁盘余量不足")
                            if not member.isfile() or not member.name.endswith(".log"):
                                raise ValueError("小时包包含非日志成员")
                            stream = archive.extractfile(member)
                            if stream is None:
                                raise OSError("归档成员不可读取")
                            decoder, pending = codecs.getincrementaldecoder("utf-8")("ignore"), ""
                            head, tail, has_newline, read = bytearray(), bytearray(), False, 0
                            while raw := stream.read(256 * 1024):
                                read += len(raw)
                                if len(head) < 16:
                                    head.extend(raw[:16 - len(head)])
                                tail[:] = (tail + raw)[-16:]
                                has_newline = has_newline or b"\n" in raw
                                # 部分SQLite词法器在NUL处停止；只替换候选文本，原文与边界保留。
                                # 含NUL的查询本就回退，替换至多增加候选，不会漏掉非NUL关键词。
                                pending += decoder.decode(raw).replace("\0", " ")
                                while len(pending) >= CHUNK_CHARS:
                                    rowid += 1
                                    connection.execute("INSERT INTO chunks(rowid, member) VALUES (?, ?)", (rowid, member.name))
                                    connection.execute("INSERT INTO terms(rowid, value) VALUES (?, ?)", (rowid, pending[:CHUNK_CHARS]))
                                    pending = pending[CHUNK_CHARS - 2:]
                            pending += decoder.decode(b"", final=True)
                            if pending:
                                rowid += 1
                                connection.execute("INSERT INTO chunks(rowid, member) VALUES (?, ?)", (rowid, member.name))
                                connection.execute("INSERT INTO terms(rowid, value) VALUES (?, ?)", (rowid, pending))
                            if read != member.size:
                                raise OSError("归档成员长度不完整")
                            connection.execute("INSERT INTO members VALUES (?, ?, ?, ?, ?)",
                                               (member.name, read, bytes(head), bytes(tail), int(has_newline)))
                connection.commit()
                if temporary.stat().st_size > MAX_INDEX_BYTES:
                    return False
            finally:
                connection.close()
            # 建库期间归档若被维护器替换，旧索引绝不能覆盖新包的候选索引。
            if _fingerprint(archive_path) != fingerprint:
                return False
            with temporary.open("rb") as index_file:
                os.fsync(index_file.fileno())
            os.replace(temporary, destination)
            directory = os.open(destination.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return True
        finally:
            temporary.unlink(missing_ok=True)
    except (OSError, sqlite3.Error, tarfile.TarError, ValueError):
        return False


def query_index(archive_path: Path, keyword: str):
    """返回保守成员候选；短词、损坏、替换或不支持 FTS 一律返回 None 供原文回退。"""
    if len(keyword) < 3 or "\x00" in keyword:
        return None
    path = index_path(Path(archive_path))
    try:
        connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            if (metadata.get("schemaVersion") != str(SCHEMA_VERSION)
                    or metadata.get("complete") != "true"
                    or metadata.get("archiveFingerprint") != _fingerprint(Path(archive_path))):
                return None
            if not _healthy(connection, path):
                return None
            trigram = keyword[:3].replace('"', '""')
            matched = {row[0] for row in connection.execute("""
                SELECT DISTINCT chunks.member
                FROM terms JOIN chunks ON chunks.rowid = terms.rowid
                WHERE terms MATCH ?
            """, (f'"{trigram}"',))}
            return {name: {"bytes": size, "head": head, "tail": tail, "hasNewline": bool(newline),
                           "mayMatch": name in matched}
                    for name, size, head, tail, newline in connection.execute("SELECT name, bytes, head, tail, has_newline FROM members")}
        finally:
            connection.close()
    except (OSError, sqlite3.Error):
        return None
