"""自动故障转移真实验证脚本的冷启动准入等待回归。"""

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from verify_node_auto_failover import archive_belongs_to_node_root, wait_node_accepting


def test_archive_belongs_to_node_root_normalizes_paths_before_checking_membership(tmp_path):
    """归档根目录判定使用规范路径，且仍拒绝另一个节点或根外文件。"""
    archive = tmp_path / "failover-a" / "task" / "archive.tar.gz"
    archive.parent.mkdir(parents=True)
    archive.touch()

    assert archive_belongs_to_node_root(archive, tmp_path, "failover-a")
    assert not archive_belongs_to_node_root(archive, tmp_path, "failover-b")
    assert not archive_belongs_to_node_root(tmp_path / "outside.tar.gz", tmp_path, "failover-a")


async def test_wait_node_accepting_waits_for_the_requested_persisted_value():
    """B 的旧准入心跳不能被误作管理员刚保存的拒绝配置。"""
    nodes = SimpleNamespace(find_one=AsyncMock(side_effect=[
        {"id": "failover-b", "accepting": True},
        {"id": "failover-b", "accepting": False},
    ]))
    database = SimpleNamespace(nodes=nodes)

    with patch("verify_node_auto_failover.asyncio.sleep", new=AsyncMock()):
        await wait_node_accepting(database, "failover-b", False)

    assert nodes.find_one.await_count == 2
