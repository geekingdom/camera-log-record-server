"""海康资源认证结果的最小可查询历史，不保存凭据或设备响应正文。"""

from camera_logs.common.database import now
from camera_logs.common.models import new_id


async def record_authentication(repo, resource, *, source: str, result: str, before=None, after=None, message=None,
                                completed_at=None, session=None):
    """写入一次已落库认证结果；调用方只传安全枚举和型号、序列号快照。"""
    before, after = before or {}, after or {}
    model_before, serial_before = (str(before.get(key) or "").strip() for key in ("model", "subSerialNumber"))
    model_after, serial_after = (str(after.get(key) or "").strip() for key in ("model", "subSerialNumber"))
    initial = result == "SUCCESS" and before.get("authenticatedAt") is None
    await repo.db.authentication_records.insert_one({
        "id": new_id(), "resourceId": resource["id"], "createdAt": completed_at or now(), "source": source, "result": result,
        "modelBefore": model_before, "modelAfter": model_after,
        "serialBefore": serial_before, "serialAfter": serial_after,
        "identityChanged": result == "SUCCESS" and not initial and (model_before, serial_before) != (model_after, serial_after),
        "initialAuthentication": initial,
        "message": message, "_id": new_id(),
    }, session=session)
