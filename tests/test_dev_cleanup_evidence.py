"""验证受限开发清理器的合成报告证据读取边界。"""

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "dev_cleanup_evidence", Path(__file__).parents[1] / "scripts/dev_cleanup_evidence.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write_evidence(directory: Path, *, passed: bool = False) -> tuple[dict, dict]:
    """写出最小双路真实协议收尾证据；性能失败不改变完整性清理资格。"""
    directory.mkdir()
    digest_one, digest_two = "a" * 64, "B" * 64
    report = {
        "scope": "real-telnet-api-worker-mongo-download", "integrityVerified": True,
        "cleanupVerified": True, "passed": passed, "routes": 2,
        "results": [
            {"route": 0, "taskId": "task-one", "sourceSha256": digest_one, "sourceLines": 12,
             "verification": {"sha256": digest_one, "lines": 12}},
            {"route": 1, "taskId": "task-two", "sourceSha256": digest_two, "sourceLines": 24,
             "verification": {"sha256": digest_two, "lines": 24}},
        ],
    }
    resource = {"resourceId": "resource-synthetic"}
    (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (directory / "cleanup.json").write_text('{"errors": []}', encoding="utf-8")
    (directory / "resource.json").write_text(json.dumps(resource), encoding="utf-8")
    (directory / "tasks.jsonl").write_text(
        '{"route":1,"taskId":"task-two","port":10002}\n'
        '{"route":0,"taskId":"task-one","port":10001}\n', encoding="utf-8")
    return report, resource


def test_load_evidence_returns_only_verified_cleanup_associations(tmp_path):
    """完整性和收尾通过时，即使性能 failed 也返回受限的资源和任务关联信息。"""
    directory = tmp_path / "synthetic"
    report, _ = write_evidence(directory, passed=False)

    result = module.load_evidence(directory)

    assert result["resourceId"] == "resource-synthetic"
    assert result["tasks"] == {"task-one": report["results"][0], "task-two": report["results"][1]}
    assert result["taskPorts"] == {"task-one": 10001, "task-two": 10002}
    assert result["reportSha256"] == hashlib.sha256((directory / "report.json").read_bytes()).hexdigest()
    assert set(result) == {"resourceId", "tasks", "taskPorts", "reportSha256"}


@pytest.mark.parametrize("change", [
    lambda directory: (directory / "cleanup.json").write_text('{"errors":["failed"]}', encoding="utf-8"),
    lambda directory: (directory / "resource.json").write_text('{"resourceId":""}', encoding="utf-8"),
    lambda directory: (directory / "tasks.jsonl").write_text(
        '{"route":0,"taskId":"task-two","port":10001}\n'
        '{"route":1,"taskId":"task-one","port":10002}\n', encoding="utf-8"),
])
def test_load_evidence_rejects_inconsistent_cleanup_associations(tmp_path, change):
    """清理、资源和逐任务记录任一不一致时，不能建立可删除的关联。"""
    directory = tmp_path / "invalid"
    write_evidence(directory)
    change(directory)

    with pytest.raises(ValueError):
        module.load_evidence(directory)


def test_load_evidence_rejects_invalid_report_symlink_and_oversized_file(tmp_path):
    """无效摘要、符号链接和超过上限的证据文件都必须拒绝。"""
    invalid = tmp_path / "invalid-report"
    report, _ = write_evidence(invalid)
    report["results"][1]["verification"]["lines"] = 25
    (invalid / "report.json").write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError):
        module.load_evidence(invalid)

    linked = tmp_path / "linked"
    write_evidence(linked)
    (linked / "cleanup.json").unlink()
    (linked / "cleanup.json").symlink_to(invalid / "cleanup.json")
    with pytest.raises(ValueError):
        module.load_evidence(linked)

    oversized = tmp_path / "oversized"
    write_evidence(oversized)
    (oversized / "tasks.jsonl").write_bytes(b"x" * (module.MAX_EVIDENCE_BYTES + 1))
    with pytest.raises(ValueError):
        module.load_evidence(oversized)
