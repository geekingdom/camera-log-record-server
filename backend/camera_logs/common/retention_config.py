"""定义管理员维护增长记录的保留策略，零值显式关闭对应维护。"""

from pydantic import BaseModel, ConfigDict, Field


class RecordRetentionConfig(BaseModel):
    """独立控制审计、运行事件和已结束运行明细，保留引用中的记录。"""

    model_config = ConfigDict(extra="forbid")
    auditDays: int = Field(default=90, ge=0, le=3650, strict=True,
                           description="业务审计保留天数；0关闭自动清理")
    eventDays: int = Field(default=90, ge=0, le=3650, strict=True,
                           description="运行事件保留天数；0关闭自动清理，活动运行引用继续保护")
    runDays: int = Field(default=90, ge=0, le=3650, strict=True,
                         description="已结束运行及终态明细保留天数；0关闭维护，未知发送及有效引用继续保护")


def retention_config(value=None):
    """为未保存过此配置的平台补齐默认值，不修改持久配置版本。"""
    return RecordRetentionConfig.model_validate(value or {}).model_dump()
