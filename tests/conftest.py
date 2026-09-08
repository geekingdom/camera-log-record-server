"""测试共享的事务接口替身，内存 Mongo 不支持会话事务。"""

import pytest


@pytest.fixture
def mock_claim_transaction(monkeypatch):
    """以空会话执行领取回调，仅供验证非事务性的调度状态语义。"""
    async def run_callback(_repo, callback):
        """MongoMock 没有会话能力；真实提交和回滚由真实 Mongo 验证脚本覆盖。"""
        return await callback(None)

    monkeypatch.setattr("camera_logs.tasks.claim.claim_transaction", run_callback)
