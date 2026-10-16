"""设备 logread 异常恢复：所有诊断和恢复输入都必须留在原采集会话。"""

import asyncio
from datetime import timedelta

import pytest
from camera_logs.collection import output_recovery
from camera_logs.collection.collector import Collector
from camera_logs.collection.command_dispatcher import CommandChannelBlocked
from camera_logs.collection.output_recovery import claim_output_recovery_lease
from camera_logs.common.config import Settings
from camera_logs.common.database import Repository, now, public
from cryptography.fernet import Fernet
from mongomock_motor import AsyncMongoMockClient


@pytest.fixture(autouse=True)
def clear_local_output_recovery_cooldown():
    """模块级冷却仅服务运行期；每个测试必须从独立节点状态开始。"""
    output_recovery._cooldowns.clear()


class Connection:
    """用单一读写队列模拟 SSH/Telnet 会话，不创建额外网络连接。"""

    def __init__(self):
        self.received = asyncio.Queue()
        self.sent = []
        self.closed = False
        self.block_output_close = False
        self.output_close_started = asyncio.Event()

    async def read(self, _size):
        return await self.received.get()

    async def write(self, data):
        self.sent.append(data)
        if data == b"ls\n":
            await self.received.put(b"ls\n/etc\n#\n")
        if data == b"outputClose\n" and self.block_output_close:
            self.output_close_started.set()
            await asyncio.Event().wait()

    async def close(self):
        self.closed = True
        await self.received.put(b"")


def task(protocol="SSH", target="HOST", resource_kind="HIKVISION_NETWORK", resource_id="resource-a"):
    """构造可辨识资源归属的最小会话任务。"""
    return {
        "id": "output-recovery", "runId": "run", "storageIdentity": "device",
        "protocol": protocol, "sshTarget": target, "ip": "192.0.2.10",
        "resourceId": resource_id, "resourceKind": resource_kind,
        "initialCommands": [], "scheduledCommands": [],
    }


def test_split_logread_exhaustion_recovers_once_after_quiet_window(tmp_path, monkeypatch):
    """跨包告警且后续静默时，只在原队列依次恢复一次。"""
    async def scenario():
        connection = Connection()
        collector = Collector(task(), tmp_path, connection_factory=lambda _: connection)
        await collector.start()
        await connection.received.put(b"bspsyslogd: the running logread nu")
        await connection.received.put(b"ms(4) are too many")
        await asyncio.sleep(.04)
        await collector.stop()
        return connection.sent

    monkeypatch.setattr("camera_logs.collection.output_recovery.OUTPUT_RECOVERY_GRACE_SECONDS", .01)
    sent = asyncio.run(scenario())
    assert sent[-3:] == [
        b"kill $(ps | grep logread | grep -v \"logread -n\" | grep -v grep | awk '{print $1}')\n",
        b"outputOpen\n",
        b"outputClose\n",
    ]


def test_continuing_output_cancels_logread_recovery(tmp_path, monkeypatch):
    """告警后继续收到业务输出时不得误杀 logread 或重开输出。"""
    async def scenario():
        connection = Connection()
        collector = Collector(task(), tmp_path, connection_factory=lambda _: connection)
        await collector.start()
        await connection.received.put(b"bspsyslogd running logread nums(4) are too many")
        await connection.received.put(b"[device] ordinary log line\n")
        await asyncio.sleep(.04)
        await collector.stop()
        return connection.sent

    monkeypatch.setattr("camera_logs.collection.output_recovery.OUTPUT_RECOVERY_GRACE_SECONDS", .01)
    assert asyncio.run(scenario())[-1:] == [b"outputClose\n"]


def test_shell_prompt_after_notice_does_not_cancel_logread_recovery(tmp_path, monkeypatch):
    """单次 shell 提示符或命令回显不是持续设备日志，不能阻止恢复。"""
    async def scenario():
        connection = Connection()
        collector = Collector(task(), tmp_path, connection_factory=lambda _: connection)
        await collector.start()
        await connection.received.put(b"bspsyslogd: the running logread nums(11) are too many")
        await connection.received.put(b"# \n")
        await connection.received.put(b"outputOpen\n")
        await asyncio.sleep(.04)
        await collector.stop()
        return connection.sent

    monkeypatch.setattr("camera_logs.collection.output_recovery.OUTPUT_RECOVERY_GRACE_SECONDS", .01)
    sent = asyncio.run(scenario())
    assert sum(b"logread" in item for item in sent) == 1
    assert sent.count(b"outputOpen\n") == 1


def test_recovery_supports_verified_ssh_slave_and_hikvision_serial(tmp_path, monkeypatch):
    """SSH 从机经身份验证后及明确海康串口会话都能控制各自设备输出。"""
    async def scenario(protocol, target, resource_kind="HIKVISION_NETWORK"):
        connection = Connection()
        collector = Collector(task(protocol, target, resource_kind), tmp_path, connection_factory=lambda _: connection)
        if protocol == "SSH" and target != "HOST":
            connection.bootstrap_verified = True
            connection.verified_shell_mode = "ASH"
            connection.verified_target = target
        await collector.start()
        await connection.received.put(b"bspsyslogd running logread nums(4) are too many")
        await asyncio.sleep(.04)
        await collector.stop()
        return connection.sent

    monkeypatch.setattr("camera_logs.collection.output_recovery.OUTPUT_RECOVERY_GRACE_SECONDS", .01)
    slave = asyncio.run(scenario("SSH", "SLAVE_1"))
    serial = asyncio.run(scenario("TELNET_SERIAL", None))
    assert any(b"logread" in item for item in slave) and b"outputOpen\n" in slave
    assert any(b"logread" in item for item in serial) and b"outputOpen\n" in serial


def test_serial_server_session_never_controls_output_without_hikvision_binding(tmp_path, monkeypatch):
    """普通串口服务器使用相同协议时，缺少海康资源标识仍不得发送输出控制命令。"""
    async def scenario():
        connection = Connection()
        collector = Collector(task("TELNET_SERIAL", None, "SERIAL_SERVER"), tmp_path,
                              connection_factory=lambda _: connection)
        await collector.start()
        await connection.received.put(b"bspsyslogd running logread nums(4) are too many")
        await asyncio.sleep(.04)
        await collector.stop()
        return connection.sent

    monkeypatch.setattr("camera_logs.collection.output_recovery.OUTPUT_RECOVERY_GRACE_SECONDS", .01)
    assert asyncio.run(scenario()) == []


def test_reader_disconnect_does_not_send_output_close(tmp_path):
    """读端断开代表会话已不可用，收尾不得再向设备写关闭命令。"""
    async def scenario():
        connection = Connection()
        collector = Collector(task(), tmp_path, connection_factory=lambda _: connection)
        await collector.start()
        await connection.received.put(b"")
        await asyncio.wait_for(collector.wait_closed(), .5)
        return connection.sent

    assert b"outputClose\n" not in asyncio.run(scenario())


def test_recovery_does_not_start_psh_channel_recovery(tmp_path, monkeypatch):
    """排队后进入 PSH 阻断状态时，诊断命令不得发送 Ctrl-C、ls 或恢复输入。"""
    async def scenario():
        connection = Connection()
        collector = Collector(task(), tmp_path, connection_factory=lambda _: connection)
        await collector.start()
        collector._commands._command_blocked = True
        collector._commands._debug.command_safe = False
        await connection.received.put(b"bspsyslogd running logread nums(4) are too many")
        await asyncio.sleep(.04)
        await collector.stop()
        return connection.sent

    monkeypatch.setattr("camera_logs.collection.output_recovery.OUTPUT_RECOVERY_GRACE_SECONDS", .01)
    sent = asyncio.run(scenario())
    assert b"\x03" not in sent
    assert b"outputOpen\n" not in sent
    assert not any(b"logread" in item for item in sent)


def test_safe_channel_rejection_keeps_collector_receiving(tmp_path, monkeypatch):
    """恢复排队后安全通道拒绝时未写设备，采集连接必须保持可用。"""
    async def scenario():
        connection = Connection()
        collector = Collector(task(), tmp_path, connection_factory=lambda _: connection)

        def reject_recovery_write():
            raise CommandChannelBlocked("PSH 命令通道尚未确认")

        await collector.start()
        collector._guard_output_recovery = reject_recovery_write
        await connection.received.put(b"bspsyslogd running logread nums(4) are too many")
        await asyncio.sleep(.04)
        closed_after_rejection = connection.closed
        sent = list(connection.sent)
        await collector.stop()
        return closed_after_rejection, sent

    monkeypatch.setattr("camera_logs.collection.output_recovery.OUTPUT_RECOVERY_GRACE_SECONDS", .01)
    closed, sent = asyncio.run(scenario())
    assert not closed
    assert not any(b"logread" in item or item == b"outputOpen\n" for item in sent)


def test_stop_times_out_output_close_and_still_closes_connection(tmp_path, monkeypatch):
    """暂停/停止的 outputClose 被设备卡住时，超时后仍主动回收同一连接。"""
    async def scenario():
        connection = Connection()
        collector = Collector(task(), tmp_path, connection_factory=lambda _: connection)
        await collector.start()
        connection.block_output_close = True
        await asyncio.wait_for(collector.stop(), .2)
        return connection.closed, connection.sent

    monkeypatch.setattr("camera_logs.collection.collector._OUTPUT_CLOSE_TIMEOUT_SECONDS", .01)
    closed, sent = asyncio.run(scenario())
    assert closed
    assert b"outputClose\n" in sent


def test_same_target_collectors_share_a_process_local_recovery_cooldown(tmp_path, monkeypatch):
    """同一 Worker 内同资源的重复会话只允许一个会话执行本次恢复。"""
    async def scenario():
        first, second = Connection(), Connection()
        one = Collector(task(), tmp_path / "one", connection_factory=lambda _: first)
        two = Collector(task(), tmp_path / "two", connection_factory=lambda _: second)
        await one.start()
        await two.start()
        notice = b"bspsyslogd running logread nums(4) are too many"
        await first.received.put(notice)
        await second.received.put(notice)
        await asyncio.sleep(.04)
        await one.stop()
        await two.stop()
        return first.sent + second.sent

    monkeypatch.setattr("camera_logs.collection.output_recovery.OUTPUT_RECOVERY_GRACE_SECONDS", .01)
    sent = asyncio.run(scenario())
    assert sum(b"logread" in item for item in sent) == 1
    assert sent.count(b"outputOpen\n") == 1


def test_different_ssh_targets_on_one_resource_have_independent_recovery_cooldowns(tmp_path, monkeypatch):
    """同一海康资源的不同已验证从机是不同 shell，不能被彼此的冷却压制。"""
    async def scenario():
        first, second = Connection(), Connection()
        for connection, target in ((first, "SLAVE_1"), (second, "SLAVE_2")):
            connection.bootstrap_verified = True
            connection.verified_shell_mode = "ASH"
            connection.verified_target = target
        one = Collector(task("SSH", "SLAVE_1"), tmp_path / "one", connection_factory=lambda _: first)
        two = Collector(task("SSH", "SLAVE_2"), tmp_path / "two", connection_factory=lambda _: second)
        await one.start()
        await two.start()
        notice = b"bspsyslogd running logread nums(4) are too many"
        await first.received.put(notice)
        await second.received.put(notice)
        await asyncio.sleep(.04)
        await one.stop()
        await two.stop()
        return first.sent + second.sent

    monkeypatch.setattr("camera_logs.collection.output_recovery.OUTPUT_RECOVERY_GRACE_SECONDS", .01)
    sent = asyncio.run(scenario())
    assert sum(b"logread" in item for item in sent) == 2
    assert sent.count(b"outputOpen\n") == 2


def test_recovery_yields_to_queued_manual_and_scheduled_commands(tmp_path, monkeypatch):
    """初始化完成后，恢复诊断仍遵循既有手动、定时、基础设施优先级。"""
    async def scenario():
        connection = Connection()
        collector = Collector(task(), tmp_path, connection_factory=lambda _: connection)
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold_session():
            entered.set()
            await release.wait()

        await collector.start()
        inflight = asyncio.create_task(collector.enqueue_manual("inflight", session_guard=hold_session))
        await entered.wait()
        scheduled = asyncio.create_task(collector._enqueue("scheduled", "\n", priority=2))
        await connection.received.put(b"bspsyslogd running logread nums(4) are too many")
        await asyncio.sleep(.02)
        manual = asyncio.create_task(collector.enqueue_manual("manual"))
        release.set()
        await asyncio.gather(inflight, scheduled, manual)
        await asyncio.sleep(.02)
        await collector.stop()
        return connection.sent

    monkeypatch.setattr("camera_logs.collection.output_recovery.OUTPUT_RECOVERY_GRACE_SECONDS", .01)
    sent = asyncio.run(scenario())
    assert sent[1:5] == [
        b"inflight\n", b"manual\n", b"scheduled\n",
        b"kill $(ps | grep logread | grep -v \"logread -n\" | grep -v grep | awk '{print $1}')\n",
    ]
    assert sent[5] == b"outputOpen\n"


def test_cross_worker_lease_coordinates_host_and_slave_targets_without_owner_renewal(tmp_path):
    """主机 shell 跨 SSH/Telnet 互斥，从机独立；未到期租约不可按 owner 续领。"""
    async def scenario():
        repo = Repository(AsyncMongoMockClient().camera_logs, Settings(
            encryption_key=Fernet.generate_key().decode(), log_root=tmp_path,
        ))
        await repo.initialize()
        await repo.db.resources.insert_one({"id": "resource-a", "deletedAt": None})
        first = task("SSH", "SLAVE_1") | {"nodeId": "node-a", "generation": 1, "port": 22}
        second = task("SSH", "SLAVE_1") | {"id": "other", "nodeId": "node-b", "generation": 2, "port": 2222}
        other_target = task("SSH", "SLAVE_2") | {"id": "third", "nodeId": "node-b", "generation": 2}
        assert await claim_output_recovery_lease(repo, first)
        assert not await claim_output_recovery_lease(repo, first)
        assert not await claim_output_recovery_lease(repo, second)
        assert await claim_output_recovery_lease(repo, other_target)
        await repo.db.resources.update_one(
            {"id": "resource-a"}, {"$set": {"outputRecoveryLeases.SSH_SLAVE_1.until": now() - timedelta(seconds=1)}},
        )
        assert await claim_output_recovery_lease(repo, second)
        host_ssh = task("SSH") | {"id": "host-ssh", "nodeId": "node-a", "generation": 1}
        host_telnet = task("TELNET_DEVICE") | {"id": "host-telnet", "nodeId": "node-b", "generation": 1}
        assert await claim_output_recovery_lease(repo, host_ssh)
        assert not await claim_output_recovery_lease(repo, host_telnet)

    asyncio.run(scenario())


def test_public_resource_hides_output_recovery_lease_owners():
    """资源接口不能返回协调租约中的任务、运行或节点身份。"""
    document = public({
        "id": "resource-a", "name": "camera",
        "outputRecoveryLeases": {"HOST": {"taskId": "task", "runId": "run", "nodeId": "node"}},
    })
    assert document == {"id": "resource-a", "name": "camera"}


def test_recovery_lease_failure_closes_current_collector_session(tmp_path, monkeypatch):
    """跨 Worker 租约超时或不可确认时关闭会话，不猜测 kill 是否可以重试。"""
    async def scenario():
        connection = Connection()

        async def unavailable_lease():
            raise TimeoutError("lease unavailable")

        collector = Collector(task(), tmp_path, connection_factory=lambda _: connection,
                              claim_output_recovery=unavailable_lease)
        await collector.start()
        await connection.received.put(b"bspsyslogd: the running logread nums(11) are too many")
        await asyncio.wait_for(collector.wait_closed(), .5)
        return connection.closed, connection.sent

    monkeypatch.setattr("camera_logs.collection.output_recovery.OUTPUT_RECOVERY_GRACE_SECONDS", .01)
    closed, sent = asyncio.run(scenario())
    assert closed
    assert not any(b"logread" in item for item in sent)


def test_recovery_lease_timeout_closes_current_collector_session(tmp_path, monkeypatch):
    """领取租约卡住超过上限时同样不可确认，不能让恢复协程无限等待。"""
    async def scenario():
        connection = Connection()

        async def unavailable_lease():
            await asyncio.Event().wait()

        collector = Collector(task(), tmp_path, connection_factory=lambda _: connection,
                              claim_output_recovery=unavailable_lease)
        await collector.start()
        await connection.received.put(b"bspsyslogd: the running logread nums(11) are too many")
        await asyncio.wait_for(collector.wait_closed(), .5)
        return connection.closed, connection.sent

    monkeypatch.setattr("camera_logs.collection.output_recovery.OUTPUT_RECOVERY_GRACE_SECONDS", .01)
    monkeypatch.setattr("camera_logs.collection.collector._OUTPUT_RECOVERY_LEASE_TIMEOUT_SECONDS", .01)
    closed, sent = asyncio.run(scenario())
    assert closed
    assert not any(b"logread" in item for item in sent)


def test_initialization_failure_without_sender_does_not_wait_for_output_close(tmp_path):
    """从机身份尚未验证时 sender 未启动，失败收尾不应等待关闭命令的超时。"""
    async def scenario():
        collector = Collector(task("SSH", "SLAVE_1"), tmp_path, connection_factory=lambda _: Connection())
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(collector.start(), .2)

    asyncio.run(scenario())
