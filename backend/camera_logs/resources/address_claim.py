"""按规范IP原子占用资源地址，避免并发创建绕过重复检查。"""

from fastapi import HTTPException


async def claim_address(repo, document, session):
    """同一事务内检查活动资源并占用唯一主键；失败时与资源、审计一起回滚。"""
    if await repo.db.resources.find_one({"ip": document["ip"], "deletedAt": None}, session=session):
        raise HTTPException(409, "该网络地址已存在设备资源，请使用已有资源")
    # _id是MongoDB内置唯一索引，并发事务只能有一个成功占用同一地址。
    await repo.db.resource_addresses.insert_one(
        {"_id": document["ip"], "resourceId": document["id"]}, session=session,
    )


async def release_address(repo, document, session):
    """软删除与地址释放共同提交，仅释放属于当前资源的地址，不碰日志目录。"""
    await repo.db.resource_addresses.delete_one(
        {"_id": document["ip"], "resourceId": document["id"]}, session=session,
    )
