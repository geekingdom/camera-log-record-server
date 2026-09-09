"""节点软删除保留日志寻址信息，以事务阻止并发新任务归属。"""

from fastapi import HTTPException
from pymongo.errors import PyMongoError

from camera_logs.common import audited_mutations
from camera_logs.common.database import now


async def delete_node(repo, actor_id, identifier, version):
    """软删除空闲节点；版本防止陈旧弹窗误删，重复请求不重复追加状态审计。"""
    async def commit(session):
        db = repo.db
        config = await db.node_configs.find_one({"id": identifier}, session=session)
        node = await db.nodes.find_one({"id": identifier}, session=session)
        if not config and not node:
            raise HTTPException(404, "节点不存在")
        if config and config.get("deletedAt"):
            if version in {config["version"], config["deleteRequestedVersion"]}:
                return
            raise HTTPException(409, "节点版本已变化，请刷新后重试")
        if version != (config["version"] if config else 0):
            raise HTTPException(409, "节点配置已变化，请刷新后重试")
        timestamp = now()
        # 与领取事务对同一 nodes 文档的写入形成冲突，避免读任务快照造成写偏斜。
        await db.nodes.update_one({"id": identifier}, {
            "$set": {"deletedAt": timestamp, "accepting": False},
            "$setOnInsert": {"id": identifier, "url": config["url"] if config else node["url"]},
        }, upsert=True, session=session)
        if ((node or {}).get("activeTasks", 0) > 0
                or await db.tasks.find_one({"nodeId": identifier}, session=session)
                or await db.runs.find_one({"nodeId": identifier, "endedAt": None}, session=session)):
            raise HTTPException(409, "节点仍有采集归属或未结束运行，请先停止相关任务并等待连接释放")
        changes = {"deletedAt": timestamp, "deleteRequestedVersion": version,
                   "accepting": False, "version": version + 1, "updatedAt": timestamp}
        if config:
            await db.node_configs.update_one({"id": identifier}, {"$set": changes}, session=session)
        else:
            await db.node_configs.insert_one({"id": identifier, "url": node["url"],
                                              "capacity": node.get("capacity", 100),
                                              "createdAt": timestamp, **changes}, session=session)
        await repo.audit(actor_id, "delete_node", identifier, session=session)

    try:
        await audited_mutations.mutation_transaction(repo, commit)
    except PyMongoError as error:
        # 只读确认原删除，不能在确认不明时重放删除或误删已重新登记的节点。
        try:
            database = audited_mutations._majority_primary_database(repo)
            confirmed = await database.node_configs.find_one({
                "id": identifier, "deletedAt": {"$ne": None}, "deleteRequestedVersion": version,
            })
        except PyMongoError:
            confirmed = None
        if confirmed is None:
            raise HTTPException(503, "节点删除结果未知，请刷新节点列表后重试") from error
