"""常用命令批量管理的独立输入合同，避免扩大共享模型模块。"""

from typing import Literal

from pydantic import Field, model_validator

from camera_logs.common.models import Model


class CommonCommandBatchItem(Model):
    """提交列表行在用户选择时看到的版本，服务端据此执行 CAS。"""

    id: str = Field(min_length=1, max_length=64, description='要批量操作的常用命令 ID；由管理员常用命令列表返回。')
    version: int = Field(ge=1, description='当前配置版本号，用于乐观锁并发修改校验。')


class CommonCommandBatchRequest(Model):
    """启用、禁用或删除最多一百条常用命令的原子请求。"""

    action: Literal["ENABLE", "DISABLE", "DELETE"] = Field(description='常用命令批量操作类型：ENABLE 启用、DISABLE 禁用、DELETE 删除；同一请求内全部目标原子提交。')
    items: list[CommonCommandBatchItem] = Field(min_length=1, max_length=100, description='待操作的常用命令及当前版本列表，1 至 100 项且 ID 不可重复；任一版本冲突会撤销整批修改。')

    @model_validator(mode="after")
    def unique_items(self):
        """同一行不能在一次 CAS 请求中出现两次，防止结果含义不确定。"""
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("批量命令不能包含重复 id")
        return self
