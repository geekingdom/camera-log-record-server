"""作业终态审计及提交不明时的产物保留回归。"""

import asyncio
from types import SimpleNamespace

import pytest
from camera_logs.common import audited_mutations
from camera_logs.logs import jobs
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
