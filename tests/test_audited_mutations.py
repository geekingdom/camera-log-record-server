"""验证审计变更事务的幂等创建、错误映射和同事务审计约定。"""

import hashlib

import pytest
from camera_logs.common.audited_mutations import (
    audited_create,
    audited_mutation,
    request_digest,
)
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository
from cryptography.fernet import Fernet
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient
from pymongo.errors import PyMongoError


@pytest.fixture
def repo(tmp_path):
    """创建不支持事务的轻量仓储，测试显式替换事务入口。"""
    database = AsyncMongoMockClient().db
    return Repository(
        database,
        Settings(log_root=tmp_path, encryption_key=Fernet.generate_key().decode()),
    )


@pytest.fixture
def direct_transaction(monkeypatch):
    """以可识别的 session 执行回调，验证所有数据库调用携带同一会话。"""
    session = object()

    async def run(_repo, callback):
        return await callback(session)

    monkeypatch.setattr("camera_logs.common.audited_mutations.mutation_transaction", run)
    return session


async def test_audited_mutation_uses_the_callback_session_for_audit(repo, direct_transaction):
    """业务回调和审计必须属于同一提交单元，审计不能在事务外补写。"""
    seen = []

    async def callback(session):
        seen.append(session)
        return {"ok": True}

    async def audit(actor, action, target, *, session=None):
        seen.extend([actor, action, target, session])

    repo.audit = audit
    assert await audited_mutation(repo, "admin", "create", "item", callback) == {"ok": True}
    assert seen == [direct_transaction, "admin", "create", "item", direct_transaction]


async def test_audit_failure_is_not_silently_omitted(repo, direct_transaction):
    """审计写入失败必须使事务回调失败，不能返回没有审计的业务结果。"""
    async def callback(_session):
        return {"ok": True}

    async def failed_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    repo.audit = failed_audit
    with pytest.raises(RuntimeError, match="audit unavailable"):
        await audited_mutation(repo, "admin", "create", "item", callback)


async def test_audited_create_prepares_once_when_transaction_callback_retries(repo, monkeypatch):
    """驱动重试事务时复用固定 id 和已准备文档，绝不重复执行事务外准备。"""
    await repo.initialize()
    calls = []

    async def retried(_repo, callback):
        await callback(None)
        # 模拟第一次事务回调被驱动中止后的数据库回滚，而非复用已提交的缓存。
        await repo.db.idempotency.delete_many({})
        await repo.db.items.delete_many({})
        await repo.db.audit.delete_many({})
        return await callback(None)

    async def prepare(identifier):
        calls.append(identifier)
        return {"id": identifier, "name": "created"}

    monkeypatch.setattr("camera_logs.common.audited_mutations.mutation_transaction", retried)
    result = await audited_create(repo, "admin", "key", "create_item", {"name": "created"}, "items", prepare)
    assert calls == [result["id"]]
    assert await repo.db.audit.count_documents({"targetId": result["id"]}) == 1


async def test_audited_create_returns_succeeded_mapping_without_prepare(repo):
    """相同成功请求读取原对象，避免再次执行可能含外部副作用的 prepare。"""
    await repo.initialize()
    payload = {"name": "created"}
    first = await audited_create(repo, "admin", "key", "create_item", payload, "items", lambda identifier: _doc(identifier))

    async def forbidden(_identifier):
        raise AssertionError("成功映射不应再次 prepare")

    assert await audited_create(repo, "admin", "key", "create_item", payload, "items", forbidden) == first


async def test_succeeded_mapping_without_a_current_object_is_gone(repo):
    """成功映射的对象被硬删除或软删除时，不能伪装成结果未知。"""
    await repo.initialize()
    payload = {"name": "created"}
    first = await audited_create(
        repo, "admin", "key", "create_item", payload, "items", _doc
    )
    await repo.db.items.delete_one({"id": first["id"]})

    with pytest.raises(HTTPException) as error:
        await audited_create(repo, "admin", "key", "create_item", payload, "items", _doc)
    assert error.value.status_code == 410


@pytest.mark.parametrize("existing", [
    {"digest": "different", "state": "SUCCEEDED"},
    {"digest": None, "state": "PENDING"},
])
async def test_audited_create_rejects_conflicting_or_unresolved_key(repo, existing):
    """同键不同载荷和未确认请求均不可重新执行 prepare。"""
    await repo.initialize()
    payload = {"name": "created"}
    digest = (
        hashlib.sha256(b"other").hexdigest()
        if existing["digest"] == "different"
        else request_digest("create_item", payload)
    )
    await repo.db.idempotency.insert_one({"actor": "admin", "key": "key", "digest": digest, "resourceId": "missing", "state": existing["state"]})

    async def forbidden(_identifier):
        raise AssertionError("冲突请求不应 prepare")

    with pytest.raises(HTTPException) as error:
        await audited_create(repo, "admin", "key", "create_item", payload, "items", forbidden)
    assert error.value.status_code == 409


async def test_audited_mutation_preserves_route_errors_and_maps_infrastructure(repo, direct_transaction, monkeypatch):
    """路由可识别错误原样上抛，基础设施失败只声明提交结果未知。"""
    async def rejected(_session):
        raise HTTPException(422, "invalid")

    with pytest.raises(HTTPException) as route_error:
        await audited_mutation(repo, "admin", "create", "item", rejected)
    assert route_error.value.status_code == 422

    async def unavailable(_repo, _callback):
        raise PyMongoError("unavailable")

    monkeypatch.setattr("camera_logs.common.audited_mutations.mutation_transaction", unavailable)
    with pytest.raises(HTTPException) as infrastructure_error:
        await audited_mutation(repo, "admin", "create", "item", rejected)
    assert infrastructure_error.value.status_code == 503
    assert "未知" in infrastructure_error.value.detail


async def test_audited_create_returns_confirmed_commit_after_ack_failure(repo, monkeypatch):
    """提交确认丢失后只读到同键成功映射和对象时才可以返回成功。"""
    await repo.initialize()

    async def lost_ack(_repo, callback):
        await callback(None)
        raise PyMongoError("commit acknowledgement lost")

    monkeypatch.setattr("camera_logs.common.audited_mutations.mutation_transaction", lost_ack)
    result = await audited_create(
        repo,
        "admin",
        "key",
        "create_item",
        {"name": "created"},
        "items",
        _doc,
    )
    assert (await repo.db.idempotency.find_one({"actor": "admin", "key": "key"}))["state"] == "SUCCEEDED"
    assert (await repo.db.items.find_one({"id": result["id"]}))["name"] == "created"


async def _doc(identifier):
    """构造测试创建文档，保持 prepare 的异步接口。"""
    return {"id": identifier, "name": "created"}
