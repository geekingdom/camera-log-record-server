"""将已封存日志组先持久化为可恢复清单，再交给有界归档调度器。"""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from pathlib import Path
from typing import Any


def pending_path(target: Path) -> Path:
    """返回与小时包同目录的恢复清单路径。"""
    return Path(str(target) + ".pending.json")


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """先同步临时文件再替换，断电时保留旧清单或完整新清单。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".partial", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def persist(target: Path, segments: list[dict[str, Any]]) -> Path:
    """为一次封存创建不可变 spool，避免与压缩发布清单竞争同一文件。"""
    path = Path(f"{target}.pending.{uuid.uuid4().hex}.json")
    write_json_atomic(path, {"formatVersion": 1, "target": str(target), "segments": segments})
    return path


def load(path: Path) -> tuple[Path, list[dict[str, Any]]]:
    """读取独立封存 spool，拒绝缺失目标或空成员的损坏记录。"""
    value = json.loads(path.read_text(encoding="utf-8"))
    target, segments = value.get("target"), value.get("segments")
    if not isinstance(target, str) or not isinstance(segments, list) or not segments:
        raise ValueError("invalid archive spool")
    return Path(target), [dict(segment) for segment in segments]
