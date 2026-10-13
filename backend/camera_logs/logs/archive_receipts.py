"""已校验归档的持久目录待办：绑定实际文件版本，重启后无需重复读取正文。"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from camera_logs.logs.archive_spool import write_json_atomic


def signature(path: Path) -> list[int]:
    """绑定当前inode、大小和纳秒修改时间，替换或修改后必须重新全文验证。"""
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def receipt_path(target: Path) -> Path:
    """每个小时包一个待办，重复发布覆盖为新版本，不新增数据库历史集合。"""
    return Path(str(target) + '.catalog.pending.json')


def persist_verified(target: Path, members: list[dict]) -> None:
    """仅由完成正文和索引摘要校验的发布器调用，先落待办再清理原文。"""
    metadata = Path(str(target) + '.metadata.json')
    indexes = {member['indexName']: signature(target.parent / member['indexName']) for member in members}
    write_json_atomic(receipt_path(target), {
        'formatVersion': 1, 'archiveSignature': signature(target),
        'metadataSignature': signature(metadata), 'indexSignatures': indexes,
    })


def load_verified(target: Path) -> tuple[list[dict], int, list[int]]:
    """只复用相同tar、metadata和索引版本的校验结果；变化或损坏回退重型恢复。"""
    receipt = receipt_path(target)
    receipt_version = signature(receipt)
    if receipt.stat().st_size > 1024 * 1024:
        raise ValueError('archive receipt too large')
    proof = json.loads(receipt.read_text(encoding='utf-8'))
    metadata = Path(str(target) + '.metadata.json')
    if (proof.get('formatVersion') != 1 or proof.get('archiveSignature') != signature(target)
            or proof.get('metadataSignature') != signature(metadata)):
        raise ValueError('archive receipt version mismatch')
    if metadata.stat().st_size > 1024 * 1024:
        raise ValueError('archive metadata too large')
    document = json.loads(metadata.read_text(encoding='utf-8'))
    members = document.get('members')
    if document.get('formatVersion') != 2 or not isinstance(members, list) or not members:
        raise ValueError('invalid archive metadata')
    indexes = proof.get('indexSignatures')
    if not isinstance(indexes, dict):
        raise TypeError('invalid archive index proof')
    for member in members:
        name = member.get('indexName')
        if not isinstance(name, str) or Path(name).name != name or not name.endswith('.index.jsonl'):
            raise ValueError('invalid archive index name')
        if indexes.get(name) != signature(target.parent / name):
            raise ValueError('archive index version mismatch')
    return members, proof['archiveSignature'][2], receipt_version


def acknowledge(target: Path, version: list[int]) -> None:
    """仅删除刚处理的版本；并发新发布的待办必须保留。"""
    path = receipt_path(target)
    claimed = path.with_name(path.name + '.' + uuid.uuid4().hex + '.ack')
    try:
        os.replace(path, claimed)
    except FileNotFoundError:
        return
    try:
        if signature(claimed) != version:
            # 先原子领取再核对，避免stat与unlink之间误删新发布版本。
            # link不覆盖；若已有更新待办则保留更新版本即可。
            try:
                os.link(claimed, path)
            except FileExistsError:
                pass
    finally:
        claimed.unlink(missing_ok=True)
