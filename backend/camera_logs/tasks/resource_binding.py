"""在任务边界验证资源与连接目标，并生成跨资源名称稳定的设备存储身份。"""

import hashlib
import json

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
    if network:
        identity = [resource["ip"], resource["model"], resource["subSerialNumber"]]
        storage = hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    else:
        storage = resource["id"]
    return {"resourceId": resource["id"], "storageIdentity": storage}
