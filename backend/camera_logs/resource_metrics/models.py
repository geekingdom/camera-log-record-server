"""校验资源监控的管理员命令与正则配置，避免动态配置扩大设备控制面。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Literal

import regex
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _single_line(value: str, label: str, maximum: int) -> str:
    """拒绝控制字符和换行，确保每项仍是采集器中的一个受控队列命令。"""
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label}不能为空且长度不能超过{maximum}")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError(f"{label}必须为单行文本，不能包含控制字符")
    return value


class _SettingsModel(BaseModel):
    """监控设置拒绝未声明字段，避免错误配置被悄悄保存。"""

    model_config = ConfigDict(extra="forbid")


class MetricItem(_SettingsModel):
    """单条设备命令输出中提取的结构化指标。"""

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=96)
    command: str = Field(min_length=1, max_length=2048)
    pattern: str = Field(min_length=1, max_length=512)
    unit: Literal["KB", "%"]
    enabled: bool = True

    @field_validator("command")
    @classmethod
    def command_is_single_line(cls, value: str) -> str:
        """每个指标命令独占一个有限响应捕获窗口。"""
        return _single_line(value, "监控命令", 2048)

    @field_validator("pattern")
    @classmethod
    def pattern_is_safe(cls, value: str) -> str:
        """保存前编译，防止运行中的节点遇到格式错误正则。"""
        try:
            compiled = regex.compile(value)
        except regex.error as error:
            raise ValueError("指标正则无效") from error
        if compiled.groups < 1:
            raise ValueError("指标正则必须包含数值捕获组")
        return value


class ProcessRule(_SettingsModel):
    """从进程列表中识别目标进程并生成稳定展示名称的规则。"""

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=96)
    pattern: str = Field(min_length=1, max_length=512)
    nameGroup: int | None = Field(default=None, ge=1, le=16)
    enabled: bool = True

    @field_validator("pattern")
    @classmethod
    def pattern_is_safe(cls, value: str) -> str:
        """与指标正则相同，配置提交时先验证语法。"""
        try:
            regex.compile(value)
        except regex.error as error:
            raise ValueError("进程正则无效") from error
        return value

    @model_validator(mode="after")
    def group_exists(self):
        """动态名称组必须实际存在，避免运行时被错误规则拖垮。"""
        if self.nameGroup is not None and regex.compile(self.pattern).groups < self.nameGroup:
            raise ValueError("进程正则未提供 nameGroup 指定的捕获组")
        return self


class ResourceMonitorConfig(_SettingsModel):
    """管理员维护的资源监控规则，首版固定一分钟采样周期。"""

    intervalSeconds: Literal[60] = 60
    retentionDays: int = Field(default=90, ge=1, le=90)
    items: list[MetricItem] = Field(default_factory=list, max_length=16)
    processDiscoveryCommand: str = Field(default="ps", min_length=1, max_length=2048)
    processRules: list[ProcessRule] = Field(default_factory=list, max_length=8)
    processStatusCommand: str = Field(default="cat /proc/{pid}/status", min_length=1, max_length=2048)
    processValuePattern: str = Field(default=r"VmRSS:\s*(\d+)\s*kB", min_length=1, max_length=512)

    @field_validator("processDiscoveryCommand")
    @classmethod
    def discovery_is_single_line(cls, value: str) -> str:
        """进程发现仍必须作为一条串行设备命令执行。"""
        return _single_line(value, "进程发现命令", 2048)

    @field_validator("processStatusCommand")
    @classmethod
    def status_template_is_safe(cls, value: str) -> str:
        """状态命令只能替换正整数 PID，不提供任意模板插值。"""
        value = _single_line(value, "进程状态命令", 2048)
        if (
            value.count("{pid}") != 1
            or "{" in value.replace("{pid}", "")
            or "}" in value.replace("{pid}", "")
        ):
            raise ValueError("进程状态命令必须且只能包含一个{pid}")
        return value

    @field_validator("processValuePattern")
    @classmethod
    def value_pattern_is_safe(cls, value: str) -> str:
        """VmRSS 提取规则需包含第一个数值捕获组。"""
        try:
            compiled = regex.compile(value)
        except regex.error as error:
            raise ValueError("进程值正则无效") from error
        if compiled.groups < 1:
            raise ValueError("进程值正则必须包含数值捕获组")
        return value

    @model_validator(mode="after")
    def unique_identifiers(self):
        """稳定 ID 是前端曲线、同一分钟覆盖和历史指标对齐的基础。"""
        for values, label in ((self.items, "指标"), (self.processRules, "进程规则")):
            identifiers = [item.id for item in values]
            if len(identifiers) != len(set(identifiers)):
                raise ValueError(f"{label} ID 不能重复")
        return self


def default_monitor_config() -> dict[str, Any]:
    """返回可序列化默认规则；调用方可安全修改副本而不污染模块常量。"""
    return {
        "intervalSeconds": 60,
        "retentionDays": 90,
        "items": [
            {
                "id": "mem-available",
                "name": "MemAvailable",
                "command": "cat /proc/meminfo",
                "pattern": r"MemAvailable:\s*(\d+)\s*kB",
                "unit": "KB",
                "enabled": True,
            },
            {
                "id": "slab",
                "name": "Slab",
                "command": "cat /proc/meminfo",
                "pattern": r"Slab:\s*(\d+)\s*kB",
                "unit": "KB",
                "enabled": True,
            },
            {
                "id": "cpu-idle",
                "name": "CPU idle",
                "command": "top -bn1",
                "pattern": r"(\d+(?:\.\d+)?)%\s*idle",
                "unit": "%",
                "enabled": True,
            },
        ],
        "processDiscoveryCommand": "ps",
        "processRules": [
            {"id": "dsp-main", "name": "Dsp_Main", "pattern": r".*/hikdsp", "enabled": True},
            {"id": "davinci", "name": "Davinci", "pattern": r".*/davinci", "enabled": True},
            {
                "id": "bll",
                "name": "bll",
                "pattern": r"/heop/package/([^/]+)/fsa/\1",
                "nameGroup": 1,
                "enabled": True,
            },
            {
                "id": "ipp",
                "name": "ipp",
                "pattern": r"\{ipp\d+_.*\} /heop/package/([^/]+)/dsp",
                "nameGroup": 1,
                "enabled": True,
            },
        ],
        "processStatusCommand": "cat /proc/{pid}/status",
        "processValuePattern": r"VmRSS:\s*(\d+)\s*kB",
    }


def parse_monitor_config(value: dict[str, Any] | None) -> dict[str, Any]:
    """合并默认值并严格校验管理员更新，返回 Mongo 可直接保存的普通字典。"""
    configured = deepcopy(default_monitor_config())
    if value:
        configured.update(value)
    return ResourceMonitorConfig.model_validate(configured).model_dump()
