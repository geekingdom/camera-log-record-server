"""读取可安全清理开发压测副本的最小证据，不访问任务日志、数据库或设备。"""

import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any

MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
SCOPES = {"real-ssh-api-worker-mongo-download", "real-telnet-api-worker-mongo-download"}
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
REQUIRED_FILES = ("report.json", "cleanup.json", "resource.json", "tasks.jsonl")


def _file_bytes(directory: Path, name: str) -> bytes:
    """只读取目录内受限大小的普通证据文件，拒绝符号链接和特殊文件。"""
    path = directory / name
    if path.is_symlink():
        raise ValueError(f"证据文件不能是符号链接: {name}")
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"证据文件不可读取: {name}") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_EVIDENCE_BYTES:
        raise ValueError(f"证据文件类型或大小无效: {name}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"证据文件不可读取: {name}") from exc


def _json_file(directory: Path, name: str) -> tuple[Any, bytes]:
    """解码单个 JSON 证据，并统一把编码或结构错误转换为拒绝原因。"""
    payload = _file_bytes(directory, name)
    try:
        return json.loads(payload), payload
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"证据 JSON 无效: {name}") from exc


def _positive_integer(value: Any) -> bool:
    """避免 bool 作为整数穿透报告中的路由和行数校验。"""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _route(value: Any) -> int:
    """路由从零开始编号，但必须是非负的实际整数。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ValueError("路由编号无效")
    return value


def _task_id(value: Any) -> str:
    """任务 ID 仅作为关联键返回，不解析或暴露任务连接配置。"""
    if not isinstance(value, str) or not value:
        raise ValueError("任务 ID 无效")
    return value


def _source_matches(result: dict[str, Any]) -> None:
    """确认逐路下载验证摘要和行数与报告源摘要完全一致。"""
    digest = result.get("sourceSha256")
    lines = result.get("sourceLines")
    verification = result.get("verification")
    if not isinstance(digest, str) or SHA256.fullmatch(digest) is None or not _positive_integer(lines):
        raise ValueError("路由源摘要或行数无效")
    if not isinstance(verification, dict) or verification.get("sha256") != digest or verification.get("lines") != lines:
        raise ValueError("逐路验证证据不一致")


def _results(report: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    """验证报告逐路结果唯一且完整，并建立 route 与 taskId 的双向索引。"""
    routes = report.get("routes")
    values = report.get("results")
    if not _positive_integer(routes) or not isinstance(values, list) or len(values) != routes:
        raise ValueError("报告路由数与结果数不一致")
    by_route: dict[int, dict[str, Any]] = {}
    by_task: dict[str, dict[str, Any]] = {}
    for result in values:
        if not isinstance(result, dict):
            raise TypeError("路由结果必须是对象")
        route = _route(result.get("route"))
        task_id = _task_id(result.get("taskId"))
        _source_matches(result)
        if route in by_route or task_id in by_task:
            raise ValueError("路由或任务 ID 重复")
        by_route[route] = result
        by_task[task_id] = result
    return by_route, by_task


def _task_records(directory: Path, expected: dict[int, dict[str, Any]]) -> dict[str, int]:
    """逐行读取无凭据任务索引，并与报告的 route/taskId 一一对应。"""
    payload = _file_bytes(directory, "tasks.jsonl")
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("任务索引编码无效") from exc
    if len(lines) != len(expected) or not lines:
        raise ValueError("任务索引行数不一致")
    ports: dict[str, int] = {}
    seen_routes: set[int] = set()
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("任务索引 JSON 无效") from exc
        if not isinstance(record, dict) or set(record) != {"route", "taskId", "port"}:
            raise ValueError("任务索引包含无关字段")
        route = _route(record["route"])
        task_id = _task_id(record["taskId"])
        port = record["port"]
        if not _positive_integer(port) or port > 65535:
            raise ValueError("任务端口无效")
        result = expected.get(route)
        if route in seen_routes or result is None or result["taskId"] != task_id or task_id in ports:
            raise ValueError("任务索引与报告不一致")
        seen_routes.add(route)
        ports[task_id] = port
    if set(seen_routes) != set(expected):
        raise ValueError("任务索引未覆盖全部路由")
    return ports


def load_evidence(directory: Path) -> dict[str, Any]:
    """验证合成压测收尾证据并返回受限清理器所需的关联映射。"""
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("证据根目录必须是非链接目录")
    for name in REQUIRED_FILES:
        _file_bytes(directory, name)
    report, report_payload = _json_file(directory, "report.json")
    cleanup, _ = _json_file(directory, "cleanup.json")
    resource, _ = _json_file(directory, "resource.json")
    if not isinstance(report, dict) or report.get("scope") not in SCOPES:
        raise ValueError("报告范围无效")
    if report.get("integrityVerified") is not True or report.get("cleanupVerified") is not True:
        raise ValueError("报告完整性或收尾未验证")
    if cleanup != {"errors": []}:
        raise ValueError("清理证据无效")
    if not isinstance(resource, dict) or set(resource) != {"resourceId"}:
        raise ValueError("资源证据无效")
    resource_id = _task_id(resource["resourceId"])
    by_route, by_task = _results(report)
    ports = _task_records(directory, by_route)
    return {"resourceId": resource_id, "tasks": by_task, "taskPorts": ports,
            "reportSha256": hashlib.sha256(report_payload).hexdigest()}
