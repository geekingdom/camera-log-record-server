"""资源共享扩展SSH端口：优先借用主机采集连接，仅从机使用扩展服务并在重连时恢复。"""

import asyncio
import logging
import time
import uuid
from datetime import timedelta

import httpx

from camera_logs.collection.psh_dialogue import PshSwitchError
from camera_logs.collection.psh_passwords import PshPasswordProvider
from camera_logs.collection.slave_events import record_slave_bootstrap_event
from camera_logs.collection.slave_shell import ShellBootstrap, SlaveLoginError
from camera_logs.collection.ssh_admission import SshSlotUncertain
from camera_logs.common.database import now
from camera_logs.common.ownership import OwnershipLost, owner_filter

logger = logging.getLogger(__name__)
PORTS = range(18080, 18085)
BOOTSTRAP_LEASE_SECONDS = 120
REMOTE_BOOTSTRAP_TIMEOUT_SECONDS = 55


class SlaveBootstrapUncertain(ConnectionError):
    """跨节点引导响应未知；租约必须保留至远端有界动作窗口结束。"""

    def __init__(self, message, *, host_task_id=None, host_node_id=None):
        super().__init__(message)
        self.host_task_id = host_task_id
        self.host_node_id = host_node_id


def dropbear_command(port):
    """只生成允许范围内固定命令；不接受调用者传入任意shell内容。"""
    if type(port) is not int or port not in PORTS:
        raise ValueError("从机扩展SSH端口必须在18080至18084之间")
    return f"/usr/sbin/dropbear -R -I 1800 -p {port}"


async def active_task(repo, config):
    """每个引导阶段核验任务运行、资源认证及期望状态，暂停后不得再建连接。"""
    task = await repo.db.tasks.find_one({**owner_filter(config), "desiredState": "RUNNING",
                                       "status": {"$nin": ["ERROR", "BLOCKED", "STOPPED", "PAUSED", "PAUSING", "STOPPING"]}})
    resource = await repo.db.resources.find_one({"id": config["resourceId"], "deletedAt": None})
    if not task or not resource or resource.get("healthStatus") in {"OFFLINE", "AUTH_FAILED", "ERROR"}:
        raise OwnershipLost("从机引导的任务或设备资源已失去运行资格")
    return resource


async def shared_port(repo, config):
    """资源文档原子固定首个扩展端口，多节点竞争不会登记不同端口或新增无界记录。"""
    resource = await active_task(repo, config)
    port = resource.get("slaveSshPort")
    if port is None:
        await repo.db.resources.update_one(
            {"id": config["resourceId"], "slaveSshPort": None, "deletedAt": None},
            {"$set": {"slaveSshPort": 18080}},
        )
        resource = await active_task(repo, config)
        port = resource.get("slaveSshPort")
    dropbear_command(port)
    return port


async def ssh_service_available(ip, port):
    """有限探测SSH横幅；探测socket始终关闭，不把普通HTTP服务当作可复用SSH。"""
    writer = None
    try:
        async with asyncio.timeout(3):
            reader, writer = await asyncio.open_connection(ip, port)
            banner = await reader.read(256)
            return banner.startswith(b"SSH-")
    except (OSError, TimeoutError):
        return False
    finally:
        if writer is not None:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 3)
            except (OSError, TimeoutError):
                pass


async def bootstrap_on_host(worker, host_id, requester, port):
    """接收节点再次核对双方资源与运行身份，借用原主机的唯一发送队列。"""
    repo = worker.repo
    resource = await active_task(repo, requester)
    if resource.get("slaveSshPort") != port or requester.get("sshTarget", "HOST") == "HOST":
        raise OwnershipLost("扩展端口与从机任务不匹配")
    runtime = worker.active.get(host_id)
    if runtime is None or runtime.task.get("resourceId") != requester["resourceId"]:
        return False
    host = runtime.task
    if host.get("protocol") != "SSH" or host.get("sshTarget", "HOST") != "HOST":
        return False
    collector = runtime.collector
    if collector is None or runtime.stopping or runtime.retired or collector._initializing:
        return False

    async def guard():
        await active_task(repo, requester)
        if not await repo.db.resources.find_one({"id": requester["resourceId"],
                "slaveSshBootstrap.token": requester.get("_bootstrapToken"),
                "slaveSshBootstrap.expiresAt": {"$gt": now()}}):
            raise OwnershipLost("借用请求的引导租约已失效")
        current = await repo.db.tasks.find_one({**owner_filter(host), "status": "COLLECTING", "desiredState": "RUNNING"})
        if not current or runtime.stopping or runtime.collector is not collector:
            raise OwnershipLost("借用的主机采集会话已退出")

    await collector.ensure_ash_for_monitor(session_guard=guard)
    # 并发创建同一监听服务时第二次bind可能失败；随后以SSH横幅重新验证实际监听。
    await collector.capture_monitor_command(dropbear_command(port) + " || true", session_guard=guard, timeout_seconds=15)
    await repo.audit("system", "slave-ssh-bootstrap-host-attempt", requester["id"])
    return True


async def borrow_host(worker, config, port):
    """跨节点定位已采集主机，内部请求只传任务身份和端口，不传口令或任意命令。"""
    repo = worker.repo
    query = {"resourceId": config["resourceId"], "protocol": "SSH", "sshTarget": {"$in": [None, "HOST"]},
             "status": "COLLECTING", "desiredState": "RUNNING"}
    async for host in repo.db.tasks.find(query).sort("id", 1):
        if host["nodeId"] == repo.settings.node_id:
            try:
                if await bootstrap_on_host(worker, host["id"], config, port):
                    await record_slave_bootstrap_event(
                        repo, config, phase="HOST_BORROWED", outcome="UNKNOWN", level="WARNING",
                        message="已通过主机采集连接提交扩展SSH引导，等待服务就绪确认", port=port,
                        host_task_id=host["id"], host_node_id=host.get("nodeId"),
                    )
                    return True
            except OwnershipLost:
                logger.warning("借用主机连接引导已失效 task=%s hostTask=%s", config["id"], host["id"])
                continue
            except (OSError, TimeoutError, ConnectionError) as error:
                raise SlaveBootstrapUncertain(
                    "本机主机引导结果未知", host_task_id=host["id"], host_node_id=host.get("nodeId"),
                ) from error
        else:
            try:
                node = await repo.db.nodes.find_one({"id": host["nodeId"]})
                if node is None:
                    continue
                payload = {"taskId": config["id"], "runId": config["runId"], "generation": config["generation"],
                           "port": port, "bootstrapToken": config["_bootstrapToken"]}
                async with httpx.AsyncClient(timeout=REMOTE_BOOTSTRAP_TIMEOUT_SECONDS, trust_env=False) as client:
                    # 取消此 await 不能吞掉 CancelledError；但请求已交给网络后远端可能
                    # 继续执行，finally必须据此保留资源租约，直到整体执行上限过去。
                    config["_bootstrapRemoteRequestStarted"] = True
                    config["_bootstrapRemoteHostTaskId"] = host["id"]
                    config["_bootstrapRemoteHostNodeId"] = host.get("nodeId")
                    response = await client.post(node["url"].rstrip("/") + f"/internal/slave-ssh/{host['id']}",
                                                 json=payload, headers={"Authorization": "Bearer " + repo.settings.internal_token})
                    if response.status_code == 200:
                        body = response.json()
                        if not isinstance(body, dict) or not isinstance(body.get("bootstrapped"), bool):
                            raise SlaveBootstrapUncertain(
                                "远端主机引导响应格式未知",
                                host_task_id=host["id"], host_node_id=host.get("nodeId"),
                            )
                        config["_bootstrapRemoteRequestStarted"] = False
                        if body["bootstrapped"]:
                            await record_slave_bootstrap_event(
                                repo, config, phase="HOST_BORROWED", outcome="UNKNOWN", level="WARNING",
                                message="远端主机已提交扩展SSH引导，等待服务就绪确认", port=port,
                                host_task_id=host["id"], host_node_id=host.get("nodeId"),
                            )
                            return True
                        # 主机运行不存在、正在初始化等明确未执行场景可以安全临时回退。
                        continue
                    # 409 只代表远端在写入前确认主机或请求资格已失效，可以安全尝试临时连接。
                    if response.status_code == 409:
                        config["_bootstrapRemoteRequestStarted"] = False
                        continue
                    raise SlaveBootstrapUncertain(
                        f"远端主机引导返回未知状态 {response.status_code}",
                        host_task_id=host["id"], host_node_id=host.get("nodeId"),
                    )
            except (OSError, TimeoutError, httpx.HTTPError, ValueError) as error:
                # 客户端超时、断连或响应损坏时，远端可能仍在主机命令队列中；不能回退再发一次。
                raise SlaveBootstrapUncertain(
                    "远端主机引导响应未知", host_task_id=host["id"], host_node_id=host.get("nodeId"),
                ) from error
    return False


async def ensure_service(worker, config, port, direct_connect):
    """每资源一个有界引导者，重启时先等主机初始化，其他从机只等待共享监听恢复。"""
    repo = worker.repo
    token = uuid.uuid4().hex
    deadline = time.monotonic() + 135
    while True:
        await active_task(repo, config)
        if await ssh_service_available(config["ip"], port):
            return
        acquired = await repo.db.resources.update_one(
            {"id": config["resourceId"], "deletedAt": None, "$or": [
                {"slaveSshBootstrap": None}, {"slaveSshBootstrap.expiresAt": {"$lt": now()}}]},
            {"$set": {"slaveSshBootstrap": {"token": token, "expiresAt": now() + timedelta(seconds=BOOTSTRAP_LEASE_SECONDS)}}},
        )
        if acquired.matched_count:
            break
        if time.monotonic() >= deadline:
            await record_slave_bootstrap_event(
                repo, config, phase="WAITING_FOR_LEASE", outcome="FAILED", level="WARNING",
                message="等待同资源从机SSH引导租约超时", port=port,
            )
            raise SlaveLoginError("等待同资源从机SSH引导超时，将稍后重试")
        await asyncio.sleep(1)

    async def guard():
        await active_task(repo, config)
        if not await repo.db.resources.find_one({"id": config["resourceId"], "slaveSshBootstrap.token": token,
                                                 "slaveSshBootstrap.expiresAt": {"$gt": now()}}):
            raise OwnershipLost("资源扩展SSH引导资格已失效")

    uncertain, attempt = False, None
    try:
        # 上限覆盖主机等待、远端管理及短连接引导，短于租约避免旧引导者无限占用。
        async with asyncio.timeout(90):
            if await ssh_service_available(config["ip"], port):
                return
            host_deadline = time.monotonic() + 20
            while time.monotonic() < host_deadline:
                await guard()
                pending = await repo.db.tasks.find_one({"resourceId": config["resourceId"], "protocol": "SSH",
                    "sshTarget": {"$in": [None, "HOST"]}, "desiredState": "RUNNING",
                    "status": {"$in": ["PENDING", "CONNECTING", "RECONNECTING", "WAITING_DEVICE"]}})
                if not pending:
                    break
                await asyncio.sleep(1)
            attempt = dict(config) | {"_bootstrapToken": token}
            borrowed = await borrow_host(worker, attempt, port)
            if not borrowed and not await ssh_service_available(config["ip"], port):
                await guard()
                connection = await direct_connect(config)
                bootstrap = ShellBootstrap(connection, config, PshPasswordProvider(repo.settings), guard)
                try:
                    await bootstrap.ensure_ash()
                    await bootstrap.command(dropbear_command(port))
                    await repo.audit("system", "slave-ssh-bootstrap-temporary-attempt", config["id"])
                    await record_slave_bootstrap_event(
                        repo, config, phase="TEMPORARY_BOOTSTRAPPED", outcome="UNKNOWN", level="WARNING",
                        message="已通过临时连接提交扩展SSH引导，等待服务就绪确认", port=port,
                    )
                finally:
                    await close_bootstrap(bootstrap, config)
            if not await ssh_service_available(config["ip"], port):
                await record_slave_bootstrap_event(
                    repo, config, phase="SERVICE_NOT_READY", outcome="FAILED", level="WARNING",
                    message="从机扩展SSH服务未就绪", port=port,
                )
                raise SlaveLoginError("从机扩展SSH服务未就绪，将在重连时重新恢复")
            await repo.audit("system", "slave-ssh-service-ready", config["id"])
            await record_slave_bootstrap_event(
                repo, config, phase="SERVICE_READY", outcome="SUCCEEDED", level="INFO",
                message="从机扩展SSH服务已就绪", port=port,
            )
    except SlaveBootstrapUncertain as error:
        # 远端执行的整体时间上限短于120秒租约。保留本token到期，避免当前或新代次在
        # 无法确认远端是否仍在主机命令队列时再创建临时bootstrap。
        uncertain = True
        await record_slave_bootstrap_event(
            repo, config, phase="REMOTE_RESULT_UNKNOWN", outcome="UNKNOWN", level="WARNING",
            message="跨节点主机引导结果未知，保留资源引导租约", port=port,
            host_task_id=error.host_task_id, host_node_id=error.host_node_id,
        )
        raise SlaveLoginError("跨节点主机引导结果未知，等待租约到期后再重试") from error
    except asyncio.CancelledError:
        if attempt and attempt.get("_bootstrapRemoteRequestStarted"):
            await record_slave_bootstrap_event(
                repo, config, phase="REMOTE_RESULT_UNKNOWN", outcome="UNKNOWN", level="WARNING",
                message="跨节点主机引导请求已取消，保留资源引导租约", port=port,
                host_task_id=attempt.get("_bootstrapRemoteHostTaskId"),
                host_node_id=attempt.get("_bootstrapRemoteHostNodeId"),
            )
        raise
    except TimeoutError:
        remote_started = bool(attempt and attempt.get("_bootstrapRemoteRequestStarted"))
        await record_slave_bootstrap_event(
            repo, config, phase="BOOTSTRAP_TIMEOUT", outcome="UNKNOWN" if remote_started else "FAILED",
            level="WARNING", message="从机SSH引导在规定时间内未完成", port=port,
            host_task_id=attempt.get("_bootstrapRemoteHostTaskId") if attempt else None,
            host_node_id=attempt.get("_bootstrapRemoteHostNodeId") if attempt else None,
        )
        raise
    finally:
        try:
            selector = {"id": config["resourceId"], "slaveSshBootstrap.token": token}
            if uncertain or (attempt and attempt.get("_bootstrapRemoteRequestStarted")):
                await repo.db.resources.update_one(selector, {"$set": {"slaveSshBootstrap.uncertainAt": now()}})
            else:
                await repo.db.resources.update_one(selector, {"$unset": {"slaveSshBootstrap": ""}})
        except Exception:
            # 清理失败留待租约过期；不得覆盖SSH关闭不确定异常并伪造安全收据。
            logger.exception("从机引导租约清理失败 resource=%s", config["resourceId"])


async def connect_slave(worker, config, direct_connect):
    """从机每次重连先恢复共享服务，握手成功才交给Collector执行初始化。"""
    repo = worker.repo
    port = await shared_port(repo, config)
    try:
        await ensure_service(worker, config, port, direct_connect)
    except PshSwitchError:
        raise SlaveLoginError("主机ASH引导未完成，当前从机会话未开始，将在后续重连重试") from None

    async def guard():
        await active_task(repo, config)

    await active_task(repo, config)
    connection = await direct_connect(dict(config) | {"port": port})
    shell = ShellBootstrap(connection, config, PshPasswordProvider(repo.settings), guard)
    try:
        await shell.enter_slave(config["sshTarget"], config["password"])
        await active_task(repo, config)
        await repo.db.tasks.update_one(owner_filter(config), {"$set": {"effectiveSshPort": port, "slaveConnectedAt": now()}})
        await repo.audit("system", "slave-ssh-connected", config["id"])
        await record_slave_bootstrap_event(
            repo, config, phase="SLAVE_CONNECTED", outcome="SUCCEEDED", level="INFO",
            message="已进入指定从机SSH会话", port=port,
        )
        return shell
    except BaseException as error:
        await close_bootstrap(shell, config)
        if isinstance(error, PshSwitchError):
            raise SlaveLoginError("主机ASH切换未完成，未执行从机初始化，将在后续重连重试") from None
        raise


async def close_bootstrap(shell, config):
    """工厂返回前的连接关闭不明也必须阻塞运行，不能以Collector尚无连接伪造释放证据。"""
    try:
        await shell.close()
    except BaseException as error:
        raise SshSlotUncertain(config["ip"], "bootstrap-close") from error
