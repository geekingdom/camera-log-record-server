"""日志目录和作业的公开投影，隔离 Worker 恢复状态与 API 响应合同。"""

from collections.abc import Mapping
from typing import Any

from camera_logs.common.database import public


def public_log_file(document: Mapping[str, Any]) -> dict[str, Any]:
    """返回日志文件公开目录；部署预备标记含恢复令牌和节点路径，仅供 Worker 收尾。"""
    result = public(document)
    for internal in ("deploymentRecovery", "sourceLogName"):
        result.pop(internal, None)
    return result


def public_job(document: Mapping[str, Any]) -> dict[str, Any]:
    """返回公开作业，并在新旧冻结文件快照中统一移除内部部署标记。"""
    result = public(document)
    files = result.get("files")
    if isinstance(files, list):
        result["files"] = [public_log_file(file) if isinstance(file, Mapping) else file for file in files]
    return result
