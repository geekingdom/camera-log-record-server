"""验证服务令牌绑定用户、实时权限和管理员管理合同。"""
# ruff: noqa: F811

from datetime import timedelta

from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.common.security import authenticate
from pymongo.errors import PyMongoError
from test_api import client  # noqa: F401


def create_user(client, username, scopes=None):
    """通过正式管理员入口建立令牌绑定用户，避免测试绕过用户模型。"""
    response = client.post("/api/v1/users", json={
        "username": username, "displayName": username + "显示名",
        "password": username + "-password-123", "scopes": scopes or [],
    })
    assert response.status_code == 201, response.text
    return response.json()


def create_token(client, user_id, *, name="集成令牌", expires_in_days=30):
    """用管理员身份创建绑定令牌，并要求响应保留用户公开信息。"""
    response = client.post("/api/v1/service-tokens", json={
        "name": name, "userId": user_id, "expiresInDays": expires_in_days,
    })
    assert response.status_code == 201, response.text
    return response.json()


def test_service_token_list_is_paginated_hides_hash_and_returns_bound_user(client):
    """管理员列表返回绑定用户信息，令牌散列和旧独立授权字段永不泄露。"""
    repo = client.app.state.repo
    user = create_user(client, "listed-user")
    client.portal.call(repo.db.tokens.insert_many, [
        {"id": "older", "name": "旧令牌", "userId": user["id"], "version": 1,
         "tokenHash": "secret-hash-older", "revoked": False,
         "expiresAt": now() + timedelta(days=10), "createdAt": now() - timedelta(days=1)},
        {"id": "newer", "name": "新令牌", "userId": user["id"], "version": 1,
         "tokenHash": "secret-hash-newer", "revoked": True,
         "expiresAt": now() + timedelta(days=20), "createdAt": now()},
    ])

    response = client.get("/api/v1/service-tokens?page=1&pageSize=1")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert body["page"] == 1
    assert body["pageSize"] == 1
    item = body["items"][0]
    assert item["id"] == "newer" and item["userId"] == user["id"]
    assert item["user"]["displayName"] == user["displayName"]
    assert {"tokenHash", "tokenEncrypted", "token", "scopes", "taskIds"}.isdisjoint(item)


def test_service_tokens_load_bound_user_permissions_live_and_record_token_audit(client):
    """权限变更、停用和撤销立即生效，审计保留用户主体和令牌标识。"""
    user = create_user(client, "live-token", ["commands:send"])
    token = create_token(client, user["id"], name="实时权限")
    headers = {"Authorization": "Bearer " + token["token"], "Idempotency-Key": "live-template"}

    assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer " + token["token"]}).status_code == 200
    created = client.post("/api/v1/command-templates", headers=headers, json={"name": "令牌模板"})
    assert created.status_code == 201, created.text
    audit = client.portal.call(client.app.state.repo.db.audit.find_one, {"action": "create_template"})
    assert audit["actor"] == user["id"]
    assert audit["serviceTokenId"] == token["id"]

    client.portal.call(client.app.state.repo.db.users.update_one, {"id": user["id"]}, {"$set": {"scopes": []}})
    identity = client.portal.call(authenticate, client.app.state.repo, token["token"])
    assert "commands:send" not in identity["scopes"]
    # 模板读写是所有有效用户的基础权限，移除显式权限后仍可继续创建个人模板。
    assert client.post("/api/v1/command-templates", headers=headers | {"Idempotency-Key": "default-template"},
                       json={"name": "默认模板"}).status_code == 201

    client.portal.call(client.app.state.repo.db.users.update_one, {"id": user["id"]}, {"$set": {"enabled": False}})
    assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer " + token["token"]}).status_code == 401

    client.portal.call(client.app.state.repo.db.users.update_one, {"id": user["id"]}, {"$set": {"enabled": True}})
    assert client.delete("/api/v1/service-tokens/" + token["id"]).status_code == 204
    assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer " + token["token"]}).status_code == 401


def test_service_token_reading_is_limited_to_bound_user_and_patch_switches_binding(client):
    """普通令牌可读取本人凭据公开信息，管理员 PATCH 后原用户立即不可见。"""
    first = create_user(client, "first-token", ["commands:send"])
    second = create_user(client, "second-token")
    token = create_token(client, first["id"], name="待编辑")
    observer = create_token(client, first["id"], name="原用户令牌")
    ordinary = {"Authorization": "Bearer " + token["token"]}
    observer_headers = {"Authorization": "Bearer " + observer["token"]}

    own_list = client.get("/api/v1/service-tokens", headers=ordinary)
    assert own_list.status_code == 200
    assert {item["id"] for item in own_list.json()["items"]} == {token["id"], observer["id"]}
    assert client.post("/api/v1/service-tokens", headers=ordinary, json={"name": "无权", "userId": second["id"]}).status_code == 403
    assert client.patch(f"/api/v1/service-tokens/{token['id']}", headers=ordinary, json={"version": 1, "name": "无权"}).status_code == 403
    assert client.delete(f"/api/v1/service-tokens/{token['id']}", headers=ordinary).status_code == 403

    changed = client.patch(f"/api/v1/service-tokens/{token['id']}", json={
        "version": token["version"], "name": "已切换", "userId": second["id"],
    })
    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] == 2 and changed.json()["userId"] == second["id"]
    identity = client.portal.call(authenticate, client.app.state.repo, token["token"])
    assert identity["id"] == second["id"]
    assert "commands:send" not in identity["scopes"]
    assert client.get("/api/v1/service-tokens", headers=ordinary).json()["items"] == [changed.json()]
    assert [item["id"] for item in client.get("/api/v1/service-tokens", headers=observer_headers).json()["items"]] == [observer["id"]]
    assert client.post(f"/api/v1/service-tokens/{token['id']}/reveal", headers=observer_headers).status_code == 403


def test_service_token_reveal_encrypts_new_token_and_allows_only_admin_or_bound_user(client):
    """创建令牌保留加密副本，公开响应不泄露，绑定用户可在权限与IP检查后查看。"""
    user = create_user(client, "reveal-owner")
    other = create_user(client, "reveal-other")
    created = create_token(client, user["id"], name="可查看令牌")
    repo = client.app.state.repo
    stored = client.portal.call(repo.db.tokens.find_one, {"id": created["id"]})

    assert stored["tokenHash"] != created["token"]
    assert stored["tokenEncrypted"] != created["token"]
    assert repo.decrypt(stored["tokenEncrypted"]) == created["token"]
    assert "tokenEncrypted" not in created
    assert client.post(f"/api/v1/service-tokens/{created['id']}/reveal").json() == {"token": created["token"]}
    audit = client.portal.call(repo.db.audit.find_one, {"action": "reveal_token", "targetId": created["id"]})
    assert audit and "token" not in audit

    owner_token = create_token(client, user["id"], name="查看身份")
    owner_headers = {"Authorization": "Bearer " + owner_token["token"]}
    assert client.post(f"/api/v1/service-tokens/{created['id']}/reveal", headers=owner_headers).json() == {"token": created["token"]}

    other_token = create_token(client, other["id"], name="他人身份")
    other_headers = {"Authorization": "Bearer " + other_token["token"]}
    assert client.post(f"/api/v1/service-tokens/{created['id']}/reveal", headers=other_headers).status_code == 403


def test_service_token_secret_responses_are_not_cached_and_reveal_rechecks_binding(client, monkeypatch):
    """口令响应禁止缓存；审计期间改绑或变更版本时不得返回先前解密的明文。"""
    owner = create_user(client, "reveal-version-owner")
    replacement = create_user(client, "reveal-version-replacement")
    created = create_token(client, owner["id"], name="版本复核")
    repo = client.app.state.repo
    assert client.post("/api/v1/service-tokens", json={
        "name": "禁止缓存", "userId": owner["id"], "expiresInDays": 1,
    }).headers["Cache-Control"] == "no-store"
    assert client.post(f"/api/v1/service-tokens/{created['id']}/reveal").headers["Cache-Control"] == "no-store"
    assert client.post(f"/api/v1/service-tokens/{created['id']}/rotate", json={
        "version": created["version"],
    }).headers["Cache-Control"] == "no-store"

    current = next(item for item in client.get("/api/v1/service-tokens").json()["items"] if item["id"] == created["id"])
    original_audit = repo.audit

    async def audit_then_rebind(*args, **kwargs):
        await original_audit(*args, **kwargs)
        await repo.db.tokens.update_one({"id": created["id"]}, {
            "$set": {"userId": replacement["id"]}, "$inc": {"version": 1},
        })

    monkeypatch.setattr(repo, "audit", audit_then_rebind)
    response = client.post(f"/api/v1/service-tokens/{created['id']}/reveal")
    assert response.status_code == 409
    assert created["token"] not in response.text
    assert current["version"] == 2


def test_service_token_reveal_handles_invalid_ciphertext_without_disclosure(client):
    """损坏密文只能触发明确服务错误，既不返回密文也不留下成功查看审计。"""
    user = create_user(client, "reveal-invalid-cipher")
    created = create_token(client, user["id"], name="损坏密文")
    repo = client.app.state.repo
    client.portal.call(repo.db.tokens.update_one, {"id": created["id"]}, {"$set": {"tokenEncrypted": "not-a-fernet-token"}})

    response = client.post(f"/api/v1/service-tokens/{created['id']}/reveal")
    assert response.status_code == 503
    assert "not-a-fernet-token" not in response.text
    assert client.portal.call(repo.db.audit.count_documents, {"action": "reveal_token", "targetId": created["id"]}) == 0


def test_service_token_reveal_requires_rotation_for_hash_only_legacy_token(client):
    """历史仅保存散列的令牌不得伪造明文，管理员必须轮换后才可查看。"""
    user = create_user(client, "legacy-token")
    repo = client.app.state.repo
    client.portal.call(repo.db.tokens.insert_one, {
        "id": "legacy-token", "name": "旧令牌", "userId": user["id"], "version": 1,
        "tokenHash": "legacy-hash", "revoked": False, "expiresAt": None, "createdAt": now(),
    })

    response = client.post("/api/v1/service-tokens/legacy-token/reveal")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SERVICE_TOKEN_LEGACY_SECRET"
    assert response.json()["error"]["message"] == "旧服务令牌未保存可恢复的明文，请使用轮换生成新令牌"


def test_service_token_reveal_does_not_disclose_secret_when_audit_fails(client, monkeypatch):
    """凭据查看的审计未持久化时返回失败，避免明文读取无审计痕迹。"""
    user = create_user(client, "reveal-audit")
    created = create_token(client, user["id"], name="审计失败")

    async def reject_audit(*args, **kwargs):
        raise PyMongoError("审计不可用")

    monkeypatch.setattr(client.app.state.repo, "audit", reject_audit)
    response = client.post(f"/api/v1/service-tokens/{created['id']}/reveal")
    assert response.status_code == 503
    assert created["token"] not in response.text


def test_service_token_rotate_invalidates_old_token_and_preserves_terminal_status(client):
    """轮换仅替换凭据，版本CAS、撤销和过期状态均不会被轮换复活。"""
    user = create_user(client, "rotate-token")
    created = create_token(client, user["id"], name="待轮换")
    rotated = client.post(f"/api/v1/service-tokens/{created['id']}/rotate", json={"version": created["version"]})

    assert rotated.status_code == 200, rotated.text
    new_token = rotated.json()["token"]
    assert new_token != created["token"]
    assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer " + created["token"]}).status_code == 401
    assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer " + new_token}).status_code == 200
    audit = client.portal.call(client.app.state.repo.db.audit.find_one, {"action": "rotate_token", "targetId": created["id"]})
    assert audit and "token" not in audit

    listed = next(item for item in client.get("/api/v1/service-tokens").json()["items"] if item["id"] == created["id"])
    assert client.post(f"/api/v1/service-tokens/{created['id']}/rotate", json={"version": created["version"]}).status_code == 409
    assert client.delete(f"/api/v1/service-tokens/{created['id']}").status_code == 204
    revoked = client.post(f"/api/v1/service-tokens/{created['id']}/rotate", json={"version": listed["version"]})
    assert revoked.status_code == 200
    assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer " + revoked.json()["token"]}).status_code == 401


def test_service_token_rotate_recovers_only_confirmed_commit_and_preserves_token_on_audit_failure(client, monkeypatch):
    """确认异常返回同一固定新令牌；未提交的审计失败不得改变旧令牌。"""
    user = create_user(client, "rotate-confirmation")
    created = create_token(client, user["id"], name="确认轮换")
    original = audited_mutations.mutation_transaction

    async def commit_then_uncertain(repo, callback):
        await original(repo, callback)
        raise PyMongoError("提交确认中断")

    monkeypatch.setattr(audited_mutations, "mutation_transaction", commit_then_uncertain)
    recovered = client.post(f"/api/v1/service-tokens/{created['id']}/rotate", json={"version": created["version"]})
    assert recovered.status_code == 200, recovered.text
    assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer " + recovered.json()["token"]}).status_code == 200

    monkeypatch.setattr(audited_mutations, "mutation_transaction", original)

    async def fail_before_transaction(repo, callback):
        raise PyMongoError("审计事务失败")

    current = next(item for item in client.get("/api/v1/service-tokens").json()["items"] if item["id"] == created["id"])
    monkeypatch.setattr(audited_mutations, "mutation_transaction", fail_before_transaction)
    failed = client.post(f"/api/v1/service-tokens/{created['id']}/rotate", json={"version": current["version"]})
    assert failed.status_code == 503
    assert client.get("/api/v1/tasks", headers={"Authorization": "Bearer " + recovered.json()["token"]}).status_code == 200


def test_token_creation_uses_audited_transaction_and_rejects_missing_user(client, monkeypatch):
    """令牌创建和审计须同事务提交，绑定不存在用户明确拒绝。"""
    original = audited_mutations.mutation_transaction
    calls = []

    async def tracked(repo, callback):
        calls.append(True)
        return await original(repo, callback)

    monkeypatch.setattr(audited_mutations, "mutation_transaction", tracked)
    user = create_user(client, "transaction-token")
    assert create_token(client, user["id"], name="审计事务")["userId"] == user["id"]
    assert calls == [True, True]
    missing = client.post("/api/v1/service-tokens", json={"name": "不存在", "userId": "missing"})
    assert missing.status_code == 422


def test_permanent_service_token_reports_live_user_status_and_can_change_expiry(client):
    """永久令牌不因空过期时间失效，用户状态与有效期编辑均在下一请求生效。"""
    user = create_user(client, "permanent-token")
    token = create_token(client, user["id"], name="永久令牌", expires_in_days=None)
    headers = {"Authorization": "Bearer " + token["token"]}

    assert token["expiresAt"] is None and token["effectiveStatus"] == "ACTIVE"
    assert client.get("/api/v1/tasks", headers=headers).status_code == 200
    repo = client.app.state.repo
    client.portal.call(repo.db.users.update_one, {"id": user["id"]}, {"$set": {"enabled": False}})
    assert client.get("/api/v1/tasks", headers=headers).status_code == 401
    listed = client.get("/api/v1/service-tokens").json()["items"]
    assert next(item for item in listed if item["id"] == token["id"])["effectiveStatus"] == "USER_DISABLED"

    client.portal.call(repo.db.users.update_one, {"id": user["id"]}, {"$set": {"enabled": True, "deletedAt": now()}})
    assert client.get("/api/v1/tasks", headers=headers).status_code == 401
    listed = client.get("/api/v1/service-tokens").json()["items"]
    assert next(item for item in listed if item["id"] == token["id"])["effectiveStatus"] == "USER_DELETED"

    client.portal.call(repo.db.users.update_one, {"id": user["id"]}, {"$set": {"deletedAt": None}})
    assert client.get("/api/v1/tasks", headers=headers).status_code == 200
    bounded = client.patch(f"/api/v1/service-tokens/{token['id']}", json={
        "version": token["version"], "expiresInDays": 1,
    })
    assert bounded.status_code == 200 and bounded.json()["expiresAt"] is not None
    permanent = client.patch(f"/api/v1/service-tokens/{token['id']}", json={
        "version": bounded.json()["version"], "expiresInDays": None,
    })
    assert permanent.status_code == 200 and permanent.json()["expiresAt"] is None


def test_expired_service_token_keeps_expired_when_editing_metadata(client):
    """已过期令牌可改显示元数据，但不能借有效期编辑或永久化恢复访问。"""
    user = create_user(client, "expired-token")
    token = create_token(client, user["id"], name="已过期令牌")
    repo = client.app.state.repo
    client.portal.call(repo.db.tokens.update_one, {"id": token["id"]}, {
        "$set": {"expiresAt": now() - timedelta(seconds=1)},
    })
    headers = {"Authorization": "Bearer " + token["token"]}

    rejected = client.patch(f"/api/v1/service-tokens/{token['id']}", json={
        "version": token["version"], "expiresInDays": None,
    })
    assert rejected.status_code == 409
    assert client.get("/api/v1/tasks", headers=headers).status_code == 401
    listed = client.get("/api/v1/service-tokens").json()["items"]
    assert next(item for item in listed if item["id"] == token["id"])["effectiveStatus"] == "EXPIRED"

    renamed = client.patch(f"/api/v1/service-tokens/{token['id']}", json={
        "version": token["version"], "name": "仍已过期",
    })
    assert renamed.status_code == 200 and renamed.json()["effectiveStatus"] == "EXPIRED"
    assert client.get("/api/v1/tasks", headers=headers).status_code == 401
