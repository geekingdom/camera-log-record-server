"""将实时缓冲缺口的两端锚点映射为已保存日志文件的有界片段。"""

import base64
import hmac
import json
from collections.abc import Iterable
from hashlib import sha256

from fastapi import HTTPException

from camera_logs.logs.order import ordered_files

MAX_CURSOR_LENGTH = 262_144


def _cursor(value: str | None, signing_key: bytes) -> dict | None:
    """解析不透明续页游标；游标只保存本次目录的校验摘要和位置。"""
    if not value:
        return None
    try:
        if len(value) > MAX_CURSOR_LENGTH:
            raise TypeError
        payload, signature = value.split(".", 1)
        expected = hmac.new(signing_key, payload.encode(), sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise TypeError
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding)
        result = json.loads(decoded)
        if not isinstance(result, dict) or not isinstance(result.get("position"), int):
            raise TypeError
        return result
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(422, "缺口目录游标无效") from None


def _encode_cursor(value: dict, signing_key: bytes) -> str:
    """生成 URL 安全的目录续页游标，不暴露存储排序字段。"""
    payload = base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    return payload + "." + hmac.new(signing_key, payload.encode(), sha256).hexdigest()


def _anchor_key(file_id: str, offset: int, session_id: str) -> dict:
    """统一锚点比较字段，防止续页请求混入另一段任务日志。"""
    return {"fileId": file_id, "offset": offset, "sessionId": session_id}


def catalog_cursor_page(cursor: str | None, anchors: dict, limit: int, signing_key: bytes) -> dict | None:
    """从首次请求冻结的片段快照续页，目录新写入不能改变已打开缺口的顺序。"""
    saved = _cursor(cursor, signing_key)
    if saved is None:
        return None
    if saved.get("anchors") != anchors or not isinstance(saved.get("snapshot"), list):
        raise HTTPException(422, "缺口目录游标与当前锚点不匹配")
    snapshot = saved["snapshot"]
    position = saved["position"]
    if position < 0 or position > len(snapshot) or not all(
        isinstance(item, dict) and isinstance(item.get("fileId"), str)
        and isinstance(item.get("start"), int) and isinstance(item.get("end"), int)
        and item["start"] >= 0 and item["end"] > item["start"] for item in snapshot
    ):
        raise HTTPException(422, "缺口目录游标位置无效")
    next_position = position + limit
    return {"items": snapshot[position:next_position], "nextCursor": _encode_cursor({
        "anchors": anchors, "position": next_position, "snapshot": snapshot,
        "unrecoverable": saved.get("unrecoverable", []),
    }, signing_key) if next_position < len(snapshot) else None, "unrecoverable": saved.get("unrecoverable", [])}


def catalog_gap_fragments(
    files: Iterable[dict], *, before_file_id: str, before_offset: int,
    before_session_id: str, after_file_id: str, after_offset: int,
    after_session_id: str, limit: int, cursor: str | None, signing_key: bytes,
) -> dict:
    """返回两端可靠偏移间已登记的字节，不把目录外内容视为可恢复。

    会话迁移不证明设备端连续，但两端及中间已保存文件仍可按全局目录顺序读取。
    页游标绑定两端锚点，调用者不能把一个已授权任务的下一页带入另一段范围。
    """
    # 节点迁移和恢复会创建新会话。会话边界不能证明设备端连续，但目录仍可
    # 返回两端及中间已经保存的文件，UI 会明确这不代表恢复了未送达服务端内容。
    anchors = {"before": _anchor_key(before_file_id, before_offset, before_session_id),
               "after": _anchor_key(after_file_id, after_offset, after_session_id)}
    saved_page = catalog_cursor_page(cursor, anchors, limit, signing_key)
    if saved_page is not None:
        return saved_page
    ordered = ordered_files(files)
    positions = {file["id"]: index for index, file in enumerate(ordered)}
    before_index = positions.get(before_file_id)
    after_index = positions.get(after_file_id)
    if before_index is None or after_index is None or before_index > after_index:
        return {"items": [], "nextCursor": None, "unrecoverable": [{
            "reason": "CATALOG_UNAVAILABLE", "message": "缺口两端未能在已保存目录中按顺序定位。",
        }]}
    before_file, after_file = ordered[before_index], ordered[after_index]
    before_bytes = before_file.get("bytes")
    after_bytes = after_file.get("bytes")
    if not all(isinstance(value, int) and value >= 0 for value in (before_bytes, after_bytes)):
        return {"items": [], "nextCursor": None, "unrecoverable": [{
            "reason": "CATALOG_UNAVAILABLE", "message": "目录缺少已确认的文件字节水位。",
        }]}
    if before_offset > before_bytes or after_offset > after_bytes or (before_index == after_index and before_offset > after_offset):
        return {"items": [], "nextCursor": None, "unrecoverable": [{
            "reason": "WATERMARK_NOT_READY", "message": "缺口边界尚未全部写入服务端目录。",
        }]}
    selected = ordered[before_index:after_index + 1]
    snapshot = []
    issues = []
    for relative, file in enumerate(selected):
        if file.get("status") == "DELETED" or not isinstance(file.get("bytes"), int) or file["bytes"] < 0:
            issues.append({"reason": "FILE_UNAVAILABLE", "message": f"文件 {file['id']} 已删除或缺少可靠字节水位，无法补读。"})
            continue
        start = before_offset if relative == 0 else 0
        end = after_offset if relative == len(selected) - 1 else file.get("bytes", 0)
        if end > start:
            snapshot.append({"fileId": file["id"], "sessionId": file.get("sessionId"),
                             "start": start, "end": end})
    if len(snapshot) > 200:
        return {"items": [], "nextCursor": None, "unrecoverable": [{
            "reason": "CATALOG_TOO_BROAD", "message": "缺口跨越的已保存片段过多，请缩小范围或通过小时归档查看。",
        }]}
    next_position = limit
    return {"items": snapshot[:limit],
            "nextCursor": _encode_cursor({"anchors": anchors, "position": next_position, "snapshot": snapshot, "unrecoverable": issues}, signing_key)
            if next_position < len(snapshot) else None,
            "unrecoverable": issues}
