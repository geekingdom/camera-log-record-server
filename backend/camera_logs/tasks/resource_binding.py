"""在任务边界验证资源与连接目标，并生成跨资源名称稳定的设备存储身份。"""

import hashlib
import json
import re

from fastapi import HTTPException


async def bind_resource(repo, task):
    """每个任务必须引用已添加资源，存储身份只能由服务端生成。"""
    resource = await repo.get("resources", task.resourceId)
    if resource.get("deletedAt"):
        raise HTTPException(409, "设备资源已删除，仅可查询已有日志")
    network = resource["kind"] == "HIKVISION_NETWORK"
    if network and not resource.get("authenticatedAt"):
        raise HTTPException(409, "海康网络设备资源尚未通过认证")
    if not network and task.protocol != "TELNET_SERIAL":
        raise HTTPException(422, "串口服务器只能创建 Telnet 串口任务")
    if not network or task.protocol != "TELNET_SERIAL":
        if task.ip != resource["ip"]:
            raise HTTPException(422, "此任务的 IP 必须与所属设备资源一致")
        if task.serialServerResourceId:
            raise HTTPException(422, "此任务不支持额外指定串口服务器资源")
    elif task.serialServerResourceId:
        server = await repo.get("resources", task.serialServerResourceId)
        if server.get("deletedAt"):
            raise HTTPException(409, "所选串口服务器资源已删除")
        if server["kind"] != "SERIAL_SERVER":
            raise HTTPException(422, "连接目标必须是串口服务器资源")
        if task.ip != server["ip"]:
            raise HTTPException(422, "任务 IP 必须与所选串口服务器一致")
    storage = storage_identity(resource)
    return {"resourceId": resource["id"], "storageIdentity": storage}


def storage_identity(resource: dict) -> str:
    """按资源快照计算任务目录身份，供认证更新与创建路径复用同一规范化规则。"""
    if resource["kind"] != "HIKVISION_NETWORK":
        return resource["id"]
    identity = [resource["ip"], (resource.get("model") or "").strip(),
                (resource.get("subSerialNumber") or "").strip()]
    # 使用可读目录名，同时保留身份摘要防止名称变化导致不同设备混写。
    readable = "-".join(_safe_component(value) for value in identity)
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()[:16]
    # 存储层将整个目录组件限制为 80 字节，必须先缩短展示部分再追加摘要。
    return f"{readable[:63].rstrip('._-')}-{digest}"


def _safe_component(value: object) -> str:
    """将设备身份转换为安全可读的目录片段，避免型号特殊字符逃逸路径。"""
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("._-")
    return (text or "unknown")[:96]
