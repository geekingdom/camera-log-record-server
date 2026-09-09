"""开发下载副本清理只接受完整证据，保护真实日志、报告和符号链接。"""

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "cleanup_dev_artifacts", Path(__file__).parents[1] / "scripts/cleanup_dev_artifacts.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def evidence(directory):
    """创建最小成功验证证据，性能未达标也不影响已验证副本清理。"""
    directory.mkdir()
    (directory / "report.json").write_text(json.dumps({
        "scope": "real-telnet-api-worker-mongo-download", "integrityVerified": True,
        "cleanupVerified": True, "passed": False, "routes": 1,
        "results": [{"route": 0, "sourceSha256": "abc", "sourceLines": 1,
                     "verification": {"sha256": "abc", "lines": 1}}],
    }))
    (directory / "cleanup.json").write_text('{"errors": []}')
    archive = directory / "route-0000-hour-000.archive"
    archive.write_bytes(b"sample")
    return archive


def test_preview_apply_and_repeat_preserve_other_data(tmp_path):
    archive = evidence(tmp_path / "experiment")
    real = tmp_path / "logs"
    real.mkdir()
    (real / "device.log").write_bytes(b"real")
    assert module.cleanup(tmp_path)["bytes"] == 6
    assert archive.exists()
    assert module.cleanup(tmp_path, apply=True)["removedFiles"] == 1
    assert not archive.exists()
    assert (archive.parent / "report.json").exists()
    assert (real / "device.log").read_bytes() == b"real"
    assert module.cleanup(tmp_path, apply=True)["removedFiles"] == 0


def test_incomplete_failed_and_symlink_evidence_are_untouched(tmp_path):
    archive = evidence(tmp_path / "failed")
    (archive.parent / "cleanup.json").write_text('{"errors": ["failed"]}')
    other = evidence(tmp_path / "linked")
    other.unlink()
    other.symlink_to(archive)
    assert module.cleanup(tmp_path, apply=True)["removedFiles"] == 0
    assert archive.exists()
    assert other.is_symlink()


def test_unknown_route_and_bad_digest_are_untouched(tmp_path):
    archive = evidence(tmp_path / "mismatch")
    record = archive.parent / "report.json"
    value = json.loads(record.read_text())
    value["results"][0]["verification"]["sha256"] = "wrong"
    record.write_text(json.dumps(value))
    unknown = evidence(tmp_path / "unknown")
    unknown.rename(unknown.with_name("route-0001-hour-000.archive"))
    assert module.cleanup(tmp_path, apply=True)["removedFiles"] == 0
