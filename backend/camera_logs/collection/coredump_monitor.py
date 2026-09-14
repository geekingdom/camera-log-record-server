"""复用采集连接检查 NFS 挂载；所有命令通过调用方的唯一发送队列。"""

import asyncio
import logging
import re
from ipaddress import ip_address
from pathlib import Path

logger = logging.getLogger(__name__)


class CoredumpMonitorLost(RuntimeError):
    """任务归属、资源健康或资源控制租约在排队期间失效。"""


def mount_target(server: str, root: str, device: str) -> tuple[Path, str]:
    """限制 shell 参数为明确绝对路径和 IP，不接受命令分隔符或目录穿越。"""
    address = ip_address(server)
    device_ip = str(ip_address(device))
    path = Path(root)
    if not path.is_absolute() or ".." in path.parts or not re.fullmatch(r"/[A-Za-z0-9_./-]+", root):
        raise ValueError("NFS 根目录必须是仅含字母数字及 _ . / - 的绝对路径")
    directory = path / device_ip
    host = f"[{address}]" if address.version == 6 else str(address)
    return directory, f"{host}:{directory}"


async def monitor_mount(send, report, target, *, interval: float = 60, guard=None, cleanup=None,
                        wait_after_false: bool = False):
    """首次切换后挂载，后续仅在 mount 未确认时重挂载；错误不终止采集。"""
    if not callable(target):
        target = lambda value=target: value
    first = True
    # False 表示当前运行已失属，None 表示同资源其它会话持有租约，应等待后再竞争。
    while guard:
        claimed = await guard()
        if claimed is False:
            if cleanup is not None:
                await cleanup.run(send, report)
            if not wait_after_false:
                return
            # 开关和认证状态会在运行中变化；保持协程等待，不建立额外设备连接。
            first = True
            await asyncio.sleep(min(interval, 5))
            continue
        if claimed:
            if cleanup is not None:
                cleanup.reopen()
            break
        # 等待方不访问设备，只需较短轮询租约以在负责人停止后及时接管。
        await asyncio.sleep(min(interval, 5))

    async def send_guard():
        """sender 真正写 socket 前再次核对，排队期间失效不能继续控制设备。"""
        if not guard or await guard():
            return
        raise CoredumpMonitorLost("coredump 监控归属或租约已失效")

    try:
        await send("debug", session_guard=send_guard)
    except Exception as error:  # noqa: BLE001 - 可选监控的错误不终止日志接收。
        # 每会话只尝试一次自动解密，避免持续错误口令触发设备 debug 锁定。
        await report("FAILED", type(error).__name__)
        return
    while True:
        try:
            # 发送前复核任务代次、资源健康和同资源租约，避免旧实例迁移后继续控制设备。
            claimed = await guard() if guard else True
            if claimed is False:
                if cleanup is not None:
                    await cleanup.run(send, report)
                if not wait_after_false:
                    return
                first = True
                await asyncio.sleep(min(interval, 5))
                continue
            if not claimed:
                await asyncio.sleep(interval)
                continue
            if cleanup is not None:
                cleanup.reopen()
            mounted = False
            if not first:
                try:
                    await send("mount", prompt=f"{target()} on ", session_guard=send_guard)
                    mounted = True
                except TimeoutError:
                    pass
            if not mounted:
                if not first:
                    # 原挂载已确认丢失，先留下重挂阶段，恢复后的 MOUNTED 不会被正常轮询去重。
                    await report("REMOUNTING", None)
                if first and cleanup is not None:
                    # 等待租约时 target 可能已切到跨节点赢家；尝试前才固定实际来源用于收尾。
                    cleanup.configure(target())
                    cleanup.mark_mount_attempted()
                await send(f"gdbcfg --password=hiklinux --nfsmount={target()} --open=1", session_guard=send_guard)
                await send("mount", prompt=f"{target()} on ", session_guard=send_guard)
            await report("MOUNTED", None)
            first = False
        except Exception as error:  # noqa: BLE001 - 命令失败隔离于采集主任务。
            # 不记录异常正文，设备命令可能携带敏感信息；取消保持向外传播。
            await report("FAILED", type(error).__name__)
        await asyncio.sleep(interval)


async def run_monitor(collector, server: str, root: str, report, *, guard=None, cleanup=None,
                      resolve_target=None, wait_after_false: bool = False):
    """目录准备失败同样只影响 coredump；监控归属于当前 collector 的生命周期。"""
    try:
        target = await resolve_target() if resolve_target is not None else None
        if target is None:
            directory, target = mount_target(server, root, collector.task["ip"])
            # 只有首个来源创建本地目录；接管旧来源不应触碰本机 NFS 根。
            if directory.parent.resolve() != directory.parent or directory.is_symlink():
                raise ValueError("NFS 目录不可使用符号链接")
            await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
        collector._coredump_target = target
        if cleanup is not None:
            cleanup.configure(target)

        async def send(command, *, prompt=None, session_guard=None, allow_during_shutdown=False):
            """监控命令低于人工命令优先级，且不会另开 SSH 连接。"""
            await collector._enqueue(command, "\n", priority=2, prompt=prompt, timeout_seconds=10,
                                     session_guard=session_guard, allow_during_shutdown=allow_during_shutdown)

        await monitor_mount(send, report, lambda: collector._coredump_target, guard=guard, cleanup=cleanup,
                            wait_after_false=wait_after_false)
    except Exception as error:  # noqa: BLE001 - 后台可选功能必须独立收尾。
        logger.warning("coredump 挂载监控不可用 task=%s error=%s", collector.task_id, type(error).__name__)
        try:
            await report("FAILED", type(error).__name__)
        except Exception:
            logger.exception("coredump 状态记录失败 task=%s", collector.task_id)


def start_monitor(collector, server: str, root: str, report, *, guard=None, cleanup_guard=None,
                  resolve_target=None, wait_after_false: bool = False) -> None:
    """为采集器创建唯一监控控制器和协程，仍由其现有 SSH 会话发送命令。"""
    from .coredump_cleanup import CoredumpMountCleanup

    if collector._initializing or collector._closed.is_set() or not collector._accepting_commands:
        return
    if collector._coredump_monitor is not None:
        return
    collector._coredump_cleanup = CoredumpMountCleanup(cleanup_guard)
    collector._coredump_report = report
    kwargs = {"guard": guard, "cleanup": collector._coredump_cleanup}
    if resolve_target is not None:
        kwargs["resolve_target"] = resolve_target
    if wait_after_false:
        kwargs["wait_after_false"] = True
    collector._coredump_monitor = asyncio.create_task(run_monitor(collector, server, root, report, **kwargs))
