"""作业终态审计及提交不明时的产物保留回归。"""

import asyncio
from concurrent.futures import CancelledError as FutureCancelledError
from datetime import timedelta
from types import SimpleNamespace

import pytest
from camera_logs.common import audited_mutations
from camera_logs.common.database import now
from camera_logs.logs import jobs
from camera_logs.logs.job_completion import complete_job
from test_api import client  # noqa: F401


def test_completion_transaction_retries_without_rebuilding_output(client, monkeypatch, tmp_path):  # noqa: F811
    """首次数据库失败后只重试终态提交，不能重新生成下载或删除原产物。"""
    repo = client.app.state.repo
    repo.settings = SimpleNamespace(log_root=tmp_path)
    job = {"id": "completion", "kind": "DOWNLOAD", "status": "RUNNING", "files": []}
    client.portal.call(repo.db.jobs.insert_one, job.copy())
    output = tmp_path / "exports" / job["id"] / "result.tar.gz"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"verified-output")
    builds, commits = [], []
    original = audited_mutations.mutation_transaction

    async def download(*args):
        builds.append(True)
        return {"resultPath": str(output), "bytes": output.stat().st_size}

    async def transient(repo, callback):
        from pymongo.errors import ConnectionFailure
        commits.append(True)
        if len(commits) == 1:
            raise ConnectionFailure("injected")
        return await original(repo, callback)

    monkeypatch.setattr(jobs, "_download", download)
    monkeypatch.setattr(audited_mutations, "mutation_transaction", transient)
    result = client.portal.call(jobs.run_job, repo, job)
    assert len(commits) == 2
    assert len(builds) == 1 and output.exists()
    assert result["status"] == "SUCCEEDED"
    assert client.portal.call(repo.db.audit.count_documents, {"action": "job_succeeded"}) == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_failure_or_cancellation_audit_matches_persisted_terminal(client, monkeypatch, tmp_path, cancel):  # noqa: F811
    """执行异常与取消都必须记录对应终态，不能把取消误报为失败。"""
    repo = client.app.state.repo
    repo.settings = SimpleNamespace(log_root=tmp_path)
    job = {"id": "terminal", "kind": "DOWNLOAD", "status": "RUNNING", "files": []}
    client.portal.call(repo.db.jobs.insert_one, job.copy())

    async def fail(*args):
        if cancel:
            raise asyncio.CancelledError()
        raise ValueError("simulated")

    monkeypatch.setattr(jobs, "_download", fail)
    result = client.portal.call(jobs.run_job, repo, job)
    expected = "CANCELLED" if cancel else "FAILED"
    assert result["status"] == expected
    assert client.portal.call(repo.db.jobs.find_one, {"id": job["id"]})["status"] == expected
    assert client.portal.call(repo.db.audit.count_documents, {"action": "job_" + expected.lower()}) == 1


def test_completion_rejects_replaced_execution_token_without_mutation_or_audit(client):  # noqa: F811
    """旧执行器不能把新 token 的运行作业提交为终态。"""
    repo = client.app.state.repo
    stored = {"id": "owned", "kind": "DOWNLOAD", "status": "RUNNING", "actor": "u",
              "nodeId": "node-new", "executionToken": "new-token",
              "leaseUntil": now() + timedelta(seconds=90)}
    submitted = stored | {"nodeId": "node-old", "executionToken": "old-token"}
    client.portal.call(repo.db.jobs.insert_one, stored)

    result = client.portal.call(complete_job, repo, submitted, {"status": "SUCCEEDED"})

    assert result == {"status": "FAILED", "error": "EXECUTION_OWNERSHIP_LOST"}
    current = client.portal.call(repo.db.jobs.find_one, {"id": "owned"})
    assert (current["status"], current["nodeId"], current["executionToken"]) == ("RUNNING", "node-new", "new-token")
    assert client.portal.call(repo.db.audit.count_documents, {"targetId": "owned"}) == 0


def test_completion_without_token_cannot_finish_token_owned_job(client):  # noqa: F811
    """旧调用方缺少 token 时不能覆盖已领取的新运行。"""
    repo = client.app.state.repo
    stored = {"id": "owned-without-token", "kind": "DOWNLOAD", "status": "RUNNING", "actor": "u",
              "nodeId": "node", "executionToken": "token", "leaseUntil": now() + timedelta(seconds=90)}
    client.portal.call(repo.db.jobs.insert_one, stored)

    result = client.portal.call(complete_job, repo, {"id": stored["id"], "nodeId": "node"}, {"status": "FAILED"})

    assert result == {"status": "FAILED", "error": "EXECUTION_OWNERSHIP_LOST"}
    current = client.portal.call(repo.db.jobs.find_one, {"id": stored["id"]})
    assert (current["status"], current["nodeId"], current["executionToken"]) == ("RUNNING", "node", "token")
    assert client.portal.call(repo.db.audit.count_documents, {"targetId": stored["id"]}) == 0


def test_completion_rejects_expired_token_owned_execution(client):  # noqa: F811
    """同 token 的迟到执行也不能在租约到期后发布成功。"""
    repo = client.app.state.repo
    stored = {"id": "expired", "kind": "DOWNLOAD", "status": "RUNNING", "actor": "u",
              "nodeId": "node", "executionToken": "token", "leaseUntil": now() - timedelta(seconds=1)}
    client.portal.call(repo.db.jobs.insert_one, stored)

    result = client.portal.call(complete_job, repo, stored.copy(), {"status": "SUCCEEDED"})

    assert result == {"status": "FAILED", "error": "EXECUTION_OWNERSHIP_LOST"}
    current = client.portal.call(repo.db.jobs.find_one, {"id": stored["id"]})
    assert (current["status"], current["nodeId"], current["executionToken"]) == ("RUNNING", "node", "token")
    assert client.portal.call(repo.db.audit.count_documents, {"targetId": stored["id"]}) == 0


def test_completion_returns_confirmed_success_even_when_old_lease_has_expired(client):  # noqa: F811
    """提交确认丢失后已可见的成功终态不能被过期租约改写为归属丢失。"""
    repo = client.app.state.repo
    stored = {"id": "confirmed", "kind": "DOWNLOAD", "status": "SUCCEEDED", "actor": "u",
              "nodeId": "node", "executionToken": "token", "leaseUntil": now() - timedelta(seconds=1)}
    client.portal.call(repo.db.jobs.insert_one, stored)

    result = client.portal.call(complete_job, repo, stored.copy(), {"status": "SUCCEEDED"})

    assert result == {"status": "SUCCEEDED"}
    assert client.portal.call(repo.db.audit.count_documents, {"targetId": stored["id"]}) == 0


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
def test_completion_returns_existing_terminal_status_without_rewrite(client, status):  # noqa: F811
    """已结束的失败和取消必须返回真实状态，旧执行器不能再写审计。"""
    repo = client.app.state.repo
    stored = {"id": f"terminal-{status}", "kind": "DOWNLOAD", "status": status, "actor": "u",
              "nodeId": "node-new", "executionToken": "new-token", "leaseUntil": now() - timedelta(seconds=1)}
    client.portal.call(repo.db.jobs.insert_one, stored)

    result = client.portal.call(complete_job, repo, stored | {"nodeId": "node-old", "executionToken": "old-token"},
                                {"status": "SUCCEEDED"})

    assert result == {"status": status}
    assert client.portal.call(repo.db.audit.count_documents, {"targetId": stored["id"]}) == 0


def test_completion_with_current_token_commits_once(client):  # noqa: F811
    """当前 token、节点和租约都匹配时只提交一次终态和审计。"""
    repo = client.app.state.repo
    stored = {"id": "current", "kind": "DOWNLOAD", "status": "RUNNING", "actor": "u",
              "nodeId": "node", "executionToken": "token", "leaseUntil": now() + timedelta(seconds=90)}
    client.portal.call(repo.db.jobs.insert_one, stored)

    result = client.portal.call(complete_job, repo, stored.copy(), {"status": "SUCCEEDED"})

    assert result == {"status": "SUCCEEDED"}
    assert client.portal.call(repo.db.jobs.find_one, {"id": stored["id"]})["status"] == "SUCCEEDED"
    assert client.portal.call(repo.db.audit.count_documents, {"targetId": stored["id"], "action": "job_succeeded"}) == 1


def test_completion_audit_failure_rolls_back_current_token_transition(client, monkeypatch):  # noqa: F811
    """当前 token 的审计异常也不能留下无审计终态。"""
    repo = client.app.state.repo
    stored = {"id": "audit-rollback", "kind": "DOWNLOAD", "status": "RUNNING", "actor": "u",
              "nodeId": "node", "executionToken": "token", "leaseUntil": now() + timedelta(seconds=90)}
    client.portal.call(repo.db.jobs.insert_one, stored)

    async def reject_audit(*_args, **_kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(repo, "audit", reject_audit)
    async def fail_after_single_attempt(_repo, callback):
        before = await _repo.db.jobs.find_one({"id": stored["id"]})
        with pytest.raises(RuntimeError, match="audit unavailable"):
            await callback(None)
        await _repo.db.jobs.replace_one({"id": stored["id"]}, before)
        raise asyncio.CancelledError()

    monkeypatch.setattr(audited_mutations, "mutation_transaction", fail_after_single_attempt)
    with pytest.raises(FutureCancelledError):
        client.portal.call(complete_job, repo, stored.copy(), {"status": "SUCCEEDED"})
    current = client.portal.call(repo.db.jobs.find_one, {"id": stored["id"]})
    assert (current["status"], current["nodeId"], current["executionToken"]) == ("RUNNING", "node", "token")
    assert client.portal.call(repo.db.audit.count_documents, {"targetId": stored["id"]}) == 0


def test_job_status_endpoints_hide_execution_ownership_fields(client):  # noqa: F811
    """下载和搜索的正式查询都不能返回内部 token、实例或租约。"""
    repo = client.app.state.repo
    for identifier, kind, endpoint in (
        ("private-download", "DOWNLOAD", "/api/v1/downloads/private-download"),
        ("private-search", "SEARCH", "/api/v1/log-searches/private-search"),
    ):
        client.portal.call(repo.db.jobs.insert_one, {
            "id": identifier, "kind": kind, "status": "RUNNING", "taskId": "fixture-device",
            "nodeId": "node", "executionToken": "private-token", "workerInstanceId": "instance",
            "leaseUntil": now() + timedelta(seconds=90),
        })
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        assert not {"executionToken", "workerInstanceId", "leaseUntil"} & response.json().keys()
