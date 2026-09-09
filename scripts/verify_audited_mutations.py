"""在随机 MongoDB 副本集库验证原子审计、幂等创建与未知提交恢复。"""

import asyncio
import json
from uuid import uuid4

from camera_logs.common import audited_mutations
from camera_logs.common.audited_mutations import audited_create, audited_mutation
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from fastapi import HTTPException
from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.errors import ConnectionFailure, DuplicateKeyError

ACTOR = "audited-mutations-verify"


def check(condition, message):
    """以中文断言报告真实事务不变量，便于 CI 定位失败类别。"""
    if not condition:
        raise AssertionError(message)


class AuditCollectionProxy:
    """只替换 audit 写入以注入失败或取消，其余真实集合调用原样透传。"""

    def __init__(self, collection, mode):
        self.collection = collection
        self.mode = mode

    async def insert_one(self, *args, **kwargs):
        """在事务内的审计插入点触发异常，验证前序业务写入同时回滚。"""
        if self.mode == "error":
            raise DuplicateKeyError("injected audit insert error")
        result = await self.collection.insert_one(*args, **kwargs)
        if self.mode == "cancel":
            raise asyncio.CancelledError()
        return result

    def __getattr__(self, name):
        """透传查询等非注入操作，保留真实副本集客户端与会话。"""
        return getattr(self.collection, name)


class DatabaseProxy:
    """仅代理 audit 集合，仓储仍用原 client 建立真实事务 session。"""

    def __init__(self, database, mode):
        self.database = database
        self.client = database.client
        self.mode = mode

    def __getattr__(self, name):
        """属性方式访问 audit 时返回注入集合。"""
        if name == "audit":
            return AuditCollectionProxy(self.database.audit, self.mode)
        return getattr(self.database, name)

    def __getitem__(self, name):
        """兼容按集合名读取，只有 audit 需要注入。"""
        if name == "audit":
            return AuditCollectionProxy(self.database.audit, self.mode)
        return self.database[name]


async def expect_audit_error_rollback(repo, database, name, callback, assertion):
    """审计插入失败时业务 callback 的全部数据库写入都必须由事务撤销。"""
    original = repo.db
    repo.db = DatabaseProxy(database, "error")
    try:
        try:
            await audited_mutation(repo, ACTOR, f"verify_{name}", name, callback)
        except DuplicateKeyError:
            pass
        else:
            raise AssertionError(f"{name} 的审计插入错误没有向上返回")
    finally:
        repo.db = original
    await assertion()
    check(await database.audit.count_documents({"action": f"verify_{name}"}) == 0,
          f"{name} 审计失败后仍留下审计事件")


async def verify_audit_error_rollbacks(repo, database):
    """审计 insert 错误必须分别回滚对象插入、CAS 更新与删除。"""
    insert_id = "audit-error-insert"

    async def insert(session):
        await database.resources.insert_one({"id": insert_id, "name": "rollback-insert", "version": 1}, session=session)

    async def inserted_is_absent():
        check(await database.resources.find_one({"id": insert_id}) is None, "审计错误后资源插入没有回滚")

    await expect_audit_error_rollback(repo, database, "insert", insert, inserted_is_absent)

    cas_id = "audit-error-cas"
    await database.templates.insert_one({"id": cas_id, "name": "before", "version": 1})

    async def cas(session):
        changed = await database.templates.find_one_and_update(
            {"id": cas_id, "version": 1}, {"$set": {"name": "after"}, "$inc": {"version": 1}},
            return_document=ReturnDocument.AFTER, session=session,
        )
        check(changed is not None, "CAS 验证夹具未命中")

    async def cas_is_unchanged():
        stored = await database.templates.find_one({"id": cas_id})
        check(stored["name"] == "before" and stored["version"] == 1, "审计错误后 CAS 更新没有回滚")

    await expect_audit_error_rollback(repo, database, "cas", cas, cas_is_unchanged)

    delete_id = "audit-error-delete"
    await database.users.insert_one({"id": delete_id, "username": "rollback-delete", "version": 1})

    async def delete(session):
        result = await database.users.delete_one({"id": delete_id}, session=session)
        check(result.deleted_count == 1, "删除验证夹具未命中")

    async def deleted_is_restored():
        check(await database.users.find_one({"id": delete_id}) is not None, "审计错误后删除没有回滚")

    await expect_audit_error_rollback(repo, database, "delete", delete, deleted_is_restored)


async def verify_audit_cancellation_rollback(repo, database):
    """审计写入后取消也必须撤销前序对象和审计事件。"""
    identifier = "audit-cancel-insert"
    original = repo.db
    repo.db = DatabaseProxy(database, "cancel")
    try:
        try:
            await audited_mutation(
                repo, ACTOR, "verify_cancel", identifier,
                lambda session: database.resources.insert_one(
                    {"id": identifier, "name": "cancelled", "version": 1}, session=session
                ),
            )
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("审计写入后的取消没有向上返回")
    finally:
        repo.db = original
    check(await database.resources.find_one({"id": identifier}) is None, "审计取消后对象插入没有回滚")
    check(await database.audit.count_documents({"action": "verify_cancel"}) == 0, "审计取消后仍留下审计事件")


async def create_document(repo, key, payload, *, prepare_calls=None):
    """使用正式 audited_create 入口创建简短夹具，不运行任何外部准备或设备访问。"""
    async def prepare(identifier):
        if prepare_calls is not None:
            prepare_calls.append(identifier)
        return {"id": identifier, "name": payload["name"], "version": 1}

    return await audited_create(repo, ACTOR, key, "verify_create", payload, "resources", prepare)


async def verify_concurrent_idempotency(repo, database):
    """并发同键创建必须只留下一个对象、一条审计和一个成功映射。"""
    key, payload = "concurrent-key", {"name": "concurrent"}
    outcomes = await asyncio.gather(
        *(create_document(repo, key, payload) for _ in range(12)), return_exceptions=True,
    )
    successful = [item for item in outcomes if isinstance(item, dict)]
    conflicts = [item for item in outcomes if isinstance(item, HTTPException) and item.status_code == 409]
    unexpected = [item for item in outcomes if not isinstance(item, dict) and item not in conflicts]
    check(not unexpected, f"并发同键创建返回意外结果：{[type(item).__name__ for item in unexpected]}")
    check(successful, "并发同键创建没有任何成功结果")
    identifiers = {item["id"] for item in successful}
    check(len(identifiers) == 1, "并发同键成功结果没有收敛到同一对象 ID")
    identifier = identifiers.pop()
    check(await database.resources.count_documents({"id": identifier}) == 1, "并发同键创建产生了重复对象")
    check(await database.audit.count_documents({"action": "verify_create", "targetId": identifier}) == 1,
          "并发同键创建产生了重复审计")
    check(await database.idempotency.count_documents({"actor": ACTOR, "key": key, "state": "SUCCEEDED"}) == 1,
          "并发同键创建没有保留唯一成功映射")
    repeated = await create_document(repo, key, payload)
    check(repeated["id"] == identifier, "成功映射快速返回没有复用已创建对象")
    check(len(conflicts) in range(13), "并发冲突分类异常")

    try:
        await create_document(repo, key, {"name": "different"})
    except HTTPException as error:
        check(error.status_code == 409, "同键不同 payload 没有返回 409")
    else:
        raise AssertionError("同键不同 payload 被错误接受")


async def verify_create_audit_failure_retry(repo, database):
    """事务审计失败不能留下 PENDING 映射；相同键随后可创建且只记一条审计。"""
    key, payload = "audit-failure-retry", {"name": "audit-failure-retry"}
    original = repo.db
    repo.db = DatabaseProxy(database, "error")
    try:
        try:
            await create_document(repo, key, payload)
        except (DuplicateKeyError, HTTPException) as error:
            if isinstance(error, HTTPException):
                check(error.status_code == 409, "审计失败创建必须拒绝且不得保留未完成映射")
        else:
            raise AssertionError("审计失败的创建没有向上返回")
    finally:
        repo.db = original
    check(await database.resources.count_documents({"name": payload["name"]}) == 0, "审计失败创建仍留下对象")
    check(await database.audit.count_documents({"action": "verify_create"}) == 1,
          "失败创建改变了既有并发场景的审计计数")
    check(await database.idempotency.count_documents({"actor": ACTOR, "key": key}) == 0,
          "审计失败回滚后残留了 PENDING 或其他幂等映射")
    created = await create_document(repo, key, payload)
    check(await database.resources.count_documents({"id": created["id"]}) == 1, "审计失败后的重试没有创建唯一对象")
    check(await database.audit.count_documents({"action": "verify_create", "targetId": created["id"]}) == 1,
          "审计失败后的重试没有产生恰好一条审计")


async def verify_template_unique_conflict_rollback(repo, database):
    """同创建者同名模板必须冲突，不同创建者可同名，失败事务不能留下第二映射或审计。"""
    payload = {"name": "unique-template", "description": "first"}

    async def prepare(identifier, description, owner):
        return {"id": identifier, "name": "unique-template", "description": description, "version": 1,
                "createdBy": owner, "createdByName": owner}

    first = await audited_create(
        repo, ACTOR, "template-unique-first", "verify_template_create", payload, "templates",
        lambda identifier: prepare(identifier, "first", ACTOR),
    )
    second = await audited_create(
        repo, "other-template-owner", "template-unique-other-owner", "verify_template_create", payload, "templates",
        lambda identifier: prepare(identifier, "other", "other-template-owner"),
    )
    try:
        await audited_create(
            repo, ACTOR, "template-unique-conflict", "verify_template_create",
            {"name": "unique-template", "description": "second"}, "templates",
            lambda identifier: prepare(identifier, "second", ACTOR),
        )
    except HTTPException as error:
        check(error.status_code == 409, "不同幂等键同名模板没有返回 409")
    else:
        raise AssertionError("不同幂等键同名模板被错误创建")
    check(await database.templates.count_documents({"name": "unique-template"}) == 2,
          "不同创建者同名模板未被保留，或同创建者冲突产生了第三个模板")
    check(await database.idempotency.count_documents({"actor": ACTOR, "key": "template-unique-conflict"}) == 0,
          "同名模板冲突后残留了第二把幂等映射")
    check(await database.audit.count_documents({"action": "verify_template_create"}) == 2,
          "同创建者同名冲突后新增了错误审计，或不同创建者创建缺少审计")
    check(await database.audit.count_documents({"action": "verify_template_create", "targetId": first["id"]}) == 1,
          "同名模板首个创建没有保留唯一审计")
    check(await database.audit.count_documents({"action": "verify_template_create", "targetId": second["id"]}) == 1,
          "不同创建者同名模板没有保留唯一审计")


async def verify_prepare_once_across_transaction_reentry(repo, database):
    """事务 callback 在真实回滚后重入时，prepare 仍只执行一次并复用固定文档。"""
    key, payload = "retry-fixed-document", {"name": "retry-fixed-document"}
    original = audited_mutations.mutation_transaction
    prepare_calls, callback_entries = [], 0

    async def aborted_then_retried(current_repo, callback):
        nonlocal callback_entries
        async with current_repo.db.client.start_session() as session:
            await session.start_transaction()
            try:
                callback_entries += 1
                await callback(session)
            finally:
                await session.abort_transaction()
        callback_entries += 1
        return await original(current_repo, callback)

    audited_mutations.mutation_transaction = aborted_then_retried
    try:
        created = await create_document(repo, key, payload, prepare_calls=prepare_calls)
    finally:
        audited_mutations.mutation_transaction = original
    check(callback_entries == 2, "事务验证没有实际让 callback 在回滚后重入")
    check(prepare_calls == [created["id"]], "事务 callback 重入时 prepare 被重复执行或固定 ID 改变")
    check(await database.resources.count_documents({"id": created["id"]}) == 1, "回滚重入创建了重复对象")
    check(await database.audit.count_documents({"action": "verify_create", "targetId": created["id"]}) == 1,
          "回滚重入创建了重复审计")


async def verify_succeeded_mapping_requires_live_object(repo, database):
    """成功映射没有对象或对象已删除时必须 410，不能重新 prepare 或谎称成功。"""
    payload = {"name": "mapping-object-boundary"}
    digest = audited_mutations.request_digest("verify_create", payload)
    for suffix, document in (("missing", None), ("deleted", {"id": "mapping-deleted", "deletedAt": True})):
        key = f"mapping-{suffix}"
        identifier = document["id"] if document else "mapping-missing"
        if document:
            await database.resources.insert_one(document)
        await database.idempotency.insert_one({
            "actor": ACTOR, "key": key, "digest": digest, "resourceId": identifier, "state": "SUCCEEDED",
        })

        async def forbidden(_identifier):
            raise AssertionError("成功映射不应再次调用 prepare")

        try:
            await audited_create(repo, ACTOR, key, "verify_create", payload, "resources", forbidden)
        except HTTPException as error:
            check(error.status_code == 410, f"{suffix} 成功映射没有返回 410")
        else:
            raise AssertionError(f"{suffix} 成功映射被错误视为可成功返回")


async def verify_unknown_commit(repo, database):
    """提交 ACK 丢失后只读确认同键对象；后续同键调用绝不复制审计或对象。"""
    key, payload = "ack-lost-key", {"name": "ack-lost"}
    original = audited_mutations.mutation_transaction
    calls = 0

    async def committed_then_ack_lost(current_repo, callback):
        nonlocal calls
        calls += 1
        await original(current_repo, callback)
        raise ConnectionFailure("injected commit acknowledgement loss")

    audited_mutations.mutation_transaction = committed_then_ack_lost
    try:
        try:
            first = await create_document(repo, key, payload)
        except HTTPException as error:
            check(error.status_code == 503, "提交 ACK 丢失未确认时必须返回 503")
            first = None
    finally:
        audited_mutations.mutation_transaction = original
    mapping = await database.idempotency.find_one({"actor": ACTOR, "key": key})
    check(calls == 1, "提交 ACK 丢失场景在函数内重复执行事务")
    check(mapping is not None and mapping["state"] == "SUCCEEDED", "ACK 丢失后没有可确认的成功映射")
    subsequent = await create_document(repo, key, payload)
    identifier = mapping["resourceId"]
    check(subsequent["id"] == identifier, "ACK 丢失后相同键没有返回已确认对象")
    if first is not None:
        check(first["id"] == identifier, "ACK 丢失时返回了错误对象")
    check(await database.resources.count_documents({"id": identifier}) == 1, "ACK 丢失重试创建了重复对象")
    check(await database.audit.count_documents({"action": "verify_create", "targetId": identifier}) == 1,
          "ACK 丢失重试创建了重复审计")


async def main():
    """连接当前本机副本集的随机临时库，任何结果均 finally 删除且不显示 URI。"""
    settings = Settings()
    database_name = f"audited_mutations_verify_{uuid4().hex}"
    check(database_name != settings.database_name, "验证脚本不能使用配置的主数据库")
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000, tz_aware=True,
                              w="majority", journal=True)
    summary = None
    try:
        database = client[database_name]
        repo = Repository(database, settings)
        await repo.initialize()
        await verify_audit_error_rollbacks(repo, database)
        await verify_audit_cancellation_rollback(repo, database)
        await verify_concurrent_idempotency(repo, database)
        await verify_create_audit_failure_retry(repo, database)
        await verify_template_unique_conflict_rollback(repo, database)
        await verify_prepare_once_across_transaction_reentry(repo, database)
        await verify_succeeded_mapping_requires_live_object(repo, database)
        await verify_unknown_commit(repo, database)
        summary = {
            "passed": True,
            "auditInsertRollback": True,
            "auditCancellationRollback": True,
            "concurrentIdempotency": True,
            "payloadConflict": True,
            "auditFailureRetry": True,
            "templateUniqueConflictRollback": True,
            "prepareOnceAcrossRetry": True,
            "succeededMappingObjectBoundary": True,
            "unknownCommitRecovery": True,
        }
    finally:
        try:
            await client.drop_database(database_name)
        finally:
            await client.close()
    print(json.dumps(summary | {"temporaryDatabaseDropped": True}))


if __name__ == "__main__":
    asyncio.run(main())
