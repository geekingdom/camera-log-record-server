"""用户资源写入围栏，阻止已删除账户留下迟到的资源或任务。"""

from fastapi import HTTPException

_SYSTEM_RESOURCE_WRITERS = frozenset({"bootstrap"})


async def claim_active_user_for_resource_write(database, user_id: str, session) -> None:
    """在创建资源或任务的事务内领取用户写入围栏。

    用户删除会修改同一用户文档。Mongo 事务因此会在二者并发时重试，并由本函数
    最终拒绝已经软删除或停用的账户，避免删除后提交新的归属资源或任务。
    """
    if user_id in _SYSTEM_RESOURCE_WRITERS:
        return
    claimed = await database.users.find_one_and_update(
        {"id": user_id, "enabled": True, "deletedAt": None},
        {"$inc": {"resourceWriteVersion": 1}},
        session=session,
    )
    if claimed is None:
        raise HTTPException(409, "用户已停用或删除，不能创建资源或任务")
