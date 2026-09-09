"""验证服务端开发归档清理只删除经真实正文核验的独占合成归档。"""

import hashlib
import importlib.util
import io
import json
import sys
import tarfile
from datetime import timedelta
from pathlib import Path

import pytest
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient

_SCRIPT = Path(__file__).parents[1] / "scripts" / "cleanup_dev_server_logs.py"


@pytest.fixture
def cleanup_module(monkeypatch):
    """以脚本目录进入模块搜索路径的方式加载被测 CLI，避免依赖真实运行环境。"""
    monkeypatch.syspath_prepend(str(_SCRIPT.parent))
    name = "cleanup_dev_server_logs_test"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def repository(root: Path) -> Repository:
    """建立隔离 MongoMock 和日志根，所有文件均由 pytest 临时目录托管。"""
    root.mkdir(parents=True, exist_ok=True)
    return Repository(
        AsyncMongoMockClient().db,
        Settings(
            log_root=root,
            node_id="development-node",
            retention_days=7,
            encryption_key=Fernet.generate_key().decode(),
        ),
    )


def source_line(sequence: int = 0) -> tuple[bytes, bytes]:
    """生成带服务器时间前缀的最小合成正文及验收器计算摘要所用的原始正文。"""
    body = f"route=0000 seq={sequence:09d} synthetic device output\n".encode()
    return b"[2026-09-09 12:00:00] " + body, body


def write_archive(root: Path, *, name: str = "synthetic-hour.tar.gz") -> tuple[Path, Path, str, str]:
    """写出只含有序日志分卷的小时包、同目录索引与 formatVersion 2 元数据。"""
    directory = root / "device" / "2026" / "09" / "09" / "12"
    directory.mkdir(parents=True, exist_ok=True)
    archive = directory / name
    log_name = "synthetic-part-000001.log"
    index_name = "synthetic-part-000001.index.jsonl"
    stored, body = source_line()
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo(log_name)
        member.size = len(stored)
        bundle.addfile(member, io.BytesIO(stored))
    index = directory / index_name
    index.write_text('{"offset":0,"receivedAt":"2026-09-09T12:00:00+00:00"}\n', encoding="utf-8")
    return archive, index, log_name, hashlib.sha256(body).hexdigest()


async def register_candidate(
    repo: Repository,
    root: Path,
    *,
    task_status: str = "STOPPED",
    desired_state: str = "STOPPED",
    archive_root: Path | None = None,
    retain_until=None,
) -> tuple[dict, Path, Path]:
    """登记一个完整、已软删除资源下的归档成员，并返回与报告等价的受限证据。"""
    archive, index, log_name, digest = write_archive(archive_root or root)
    await repo.db.resources.insert_one({
        "id": "synthetic-resource", "deletedAt": now(), "deletionState": "DONE",
    })
    await repo.db.tasks.insert_one({
        "id": "synthetic-task", "resourceId": "synthetic-resource", "resourceDeleted": True,
        "status": task_status, "desiredState": desired_state, "nodeId": None, "port": 10001,
    })
    await repo.db.files.insert_one({
        "id": "synthetic-file", "taskId": "synthetic-task", "runId": "run-1", "sessionId": "session-1",
        "nodeId": "development-node", "status": "READY", "hour": now().isoformat(),
        "path": str(archive), "indexPath": str(index), "archiveMember": log_name,
        **({"retainUntil": retain_until} if retain_until is not None else {}),
    })
    metadata = {
        "formatVersion": 2,
        "members": [{"taskId": "synthetic-task", "runId": "run-1", "sessionId": "session-1", "logName": log_name}],
    }
    Path(str(archive) + ".metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    evidence = {
        "reportSha256": "report-proof",
        "resourceId": "synthetic-resource",
        "taskPorts": {"synthetic-task": 10001},
        "tasks": {"synthetic-task": {"sourceSha256": digest, "sourceLines": 1}},
    }
    return evidence, archive, index


async def test_cleanup_report_previews_then_deletes_only_real_verified_exclusive_archive(tmp_path, cleanup_module, monkeypatch):
    """预览不得改动，apply 需经真实 tar 正文校验后删除归档、索引、元数据和目录记录。"""
    repo = repository(tmp_path)
    evidence, archive, index = await register_candidate(repo, tmp_path)
    monkeypatch.setattr(cleanup_module, "load_evidence", lambda _directory: evidence)

    preview = await cleanup_module.cleanup_report(repo, tmp_path / "report", apply=False)

    assert preview["result"] == "ELIGIBLE"
    assert archive.exists() and index.exists() and Path(str(archive) + ".metadata.json").exists()
    assert await repo.db.files.find_one({"id": "synthetic-file"}) is not None

    applied = await cleanup_module.cleanup_report(repo, tmp_path / "report", apply=True)

    assert applied["result"] == "FINISHED"
    assert applied["cleanup"] == {"removed": 1, "skipped": 0, "failures": 0}
    assert not archive.exists() and not index.exists() and not Path(str(archive) + ".metadata.json").exists()
    assert await repo.db.files.find_one({"id": "synthetic-file"}) is None


async def test_cleanup_report_future_download_protection_blocks_apply(tmp_path, cleanup_module, monkeypatch):
    """任何未来 retainUntil 都必须保留完整归档组，不能因开发模式绕过下载保护。"""
    repo = repository(tmp_path)
    evidence, archive, _ = await register_candidate(repo, tmp_path, retain_until=now() + timedelta(hours=1))
    monkeypatch.setattr(cleanup_module, "load_evidence", lambda _directory: evidence)

    result = await cleanup_module.cleanup_report(repo, tmp_path / "report", apply=True)

    assert result["result"] == "PROTECTED"
    assert result["protected"] and archive.exists()
    assert await repo.db.files.find_one({"id": "synthetic-file", "status": "READY"}) is not None


async def test_plan_cleanup_rejects_shared_archive_member_missing_from_report(tmp_path, cleanup_module):
    """同一 tar.gz 中出现报告范围外的未知成员时，不能只清理已知文件 ID。"""
    repo = repository(tmp_path)
    evidence, archive, _ = await register_candidate(repo, tmp_path)
    unknown_index = archive.parent / "unknown-part-000001.index.jsonl"
    unknown_index.write_text('{"offset":0}\n', encoding="utf-8")
    await repo.db.files.insert_one({
        "id": "unknown-file", "taskId": "unknown-task", "runId": "run-2", "sessionId": "session-2",
        "nodeId": "development-node", "status": "READY", "hour": now().isoformat(),
        "path": str(archive), "indexPath": str(unknown_index), "archiveMember": "unknown-part-000001.log",
    })

    with pytest.raises(ValueError, match="共享归档"):
        await cleanup_module.plan_cleanup(repo, evidence)


async def test_plan_cleanup_rejects_archive_path_outside_log_root(tmp_path, cleanup_module):
    """报告即使自洽，目录记录指向日志根外的路径也不得进入任何读取或删除流程。"""
    repo = repository(tmp_path / "logs")
    evidence, archive, _ = await register_candidate(repo, tmp_path / "logs", archive_root=tmp_path / "outside")

    with pytest.raises(ValueError, match="日志根目录"):
        await cleanup_module.plan_cleanup(repo, evidence)

    assert archive.exists()


async def test_plan_cleanup_rejects_active_resource_task(tmp_path, cleanup_module):
    """资源软删除尚未让任务停止并释放节点时，服务端开发清理必须拒绝。"""
    repo = repository(tmp_path)
    evidence, archive, _ = await register_candidate(
        repo, tmp_path, task_status="COLLECTING", desired_state="RUNNING",
    )

    with pytest.raises(ValueError, match="停止状态"):
        await cleanup_module.plan_cleanup(repo, evidence)

    assert archive.exists()


async def test_plan_cleanup_rejects_summary_that_does_not_match_real_tar_body(tmp_path, cleanup_module):
    """目录元数据和报告摘要不一致时必须由真实 verify_download 拒绝，不信任历史成功字段。"""
    repo = repository(tmp_path)
    evidence, archive, _ = await register_candidate(repo, tmp_path)
    evidence["tasks"]["synthetic-task"]["sourceSha256"] = "0" * 64

    with pytest.raises(AssertionError, match="摘要或行数"):
        await cleanup_module.plan_cleanup(repo, evidence)

    assert archive.exists()
