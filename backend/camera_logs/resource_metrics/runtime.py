"""复用活跃采集会话采样资源 CPU/内存，保持单连接、单接收器和资源级唯一租约。"""

from __future__ import annotations

import asyncio
import math
import time
from datetime import timedelta
from typing import Any

import regex
from pymongo import ReturnDocument

from camera_logs.common.database import now
from camera_logs.common.ownership import owner_filter
from camera_logs.resource_metrics.models import parse_monitor_config
from camera_logs.resource_metrics.store import ResourceMetricStore

_REGEX_TOTAL_SECONDS = 0.4
_REGEX_EACH_SECONDS = 0.05
_MAX_PROCESSES = 64


class ResourceMonitorUnavailable(RuntimeError):
    """本会话失去授权或采样失败；code 只允许安全分类，不带设备正文。"""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


async def _guard(runtime: Any, collector: Any) -> dict[str, Any] | None:
    """以资源 CAS 续租唯一监控权，旧会话、暂停和资源失效都不能继续发送命令。"""
    if (
        runtime.stopping
        or runtime.retired
        or runtime.collector is not collector
        or collector._closed.is_set()
    ):
        return None
    task = runtime.task
    if task.get("protocol") not in {"SSH", "TELNET_DEVICE"}:
        return None
    # 历史 SSH 任务缺省即主机；从机连接只能采集其任务日志，不能发送资源级监控命令。
    if task.get("protocol") == "SSH" and task.get("sshTarget", "HOST") != "HOST":
        return None
    current = await runtime.repo.db.tasks.find_one(
        {**owner_filter(task), "desiredState": "RUNNING", "status": "COLLECTING"}, {"id": 1}
    )
    if current is None:
        return None
    stamp = now()
    resource = await runtime.repo.db.resources.find_one_and_update(
        {
            "id": task.get("resourceId"),
            "deletedAt": None,
            "healthStatus": "ONLINE",
            "enableResourceMonitor": True,
            "$or": [
                {"resourceMonitorLeaseUntil": {"$exists": False}},
                {"resourceMonitorLeaseUntil": {"$lte": stamp}},
                {
                    "resourceMonitorLeaseTaskId": task["id"],
                    "resourceMonitorLeaseRunId": task["runId"],
                    "resourceMonitorLeaseGeneration": task.get("generation"),
                    "resourceMonitorLeaseNodeId": task.get("nodeId"),
                },
            ],
        },
        {
            "$set": {
                "resourceMonitorLeaseTaskId": task["id"],
                "resourceMonitorLeaseRunId": task["runId"],
                "resourceMonitorLeaseGeneration": task.get("generation"),
                "resourceMonitorLeaseNodeId": task.get("nodeId"),
                "resourceMonitorLeaseUntil": stamp + timedelta(seconds=70),
                "resourceMonitorCheckedAt": stamp,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    return resource


async def release_resource_monitor_lease(repo: Any, task: dict[str, Any]) -> None:
    """仅释放本运行持有的租约，旧会话永远不能清除后继采样者。"""
    await repo.db.resources.update_one(
        {
            "id": task.get("resourceId"),
            "resourceMonitorLeaseTaskId": task["id"],
            "resourceMonitorLeaseRunId": task["runId"],
            "resourceMonitorLeaseGeneration": task.get("generation"),
            "resourceMonitorLeaseNodeId": task.get("nodeId"),
        },
        {"$set": {"resourceMonitorLeaseUntil": now()}},
    )


def _number(match: regex.Match, *, unit: str) -> float | int:
    """拒绝 NaN、无穷和异常范围，避免配置正则把任意文本写成趋势数值。"""
    value = float(match.group(1))
    if not math.isfinite(value) or value < 0 or (unit == "%" and value > 100):
        raise ResourceMonitorUnavailable("指标数值无效")
    return int(value) if unit == "KB" else round(value, 3)


def _match(pattern: str, text: str, budget: list[float]) -> regex.Match | None:
    """所有管理员正则共享本轮总预算，单条规则最多占用50ms。"""
    remaining = _REGEX_TOTAL_SECONDS - budget[0]
    if remaining <= 0:
        raise ResourceMonitorUnavailable("REGEX_BUDGET_EXHAUSTED")
    try:
        started = time.monotonic()
        matched = regex.search(pattern, text, timeout=min(_REGEX_EACH_SECONDS, remaining))
    except TimeoutError as error:
        raise ResourceMonitorUnavailable("REGEX_TIMEOUT") from error
    finally:
        budget[0] += time.monotonic() - started
    if budget[0] > _REGEX_TOTAL_SECONDS:
        raise ResourceMonitorUnavailable("REGEX_BUDGET_EXHAUSTED")
    return matched


async def _capture(collector: Any, command: str, guard) -> str:
    """调用现有队列的有限旁路捕获；任何异常只向监控本轮传播。"""
    raw = await collector.capture_monitor_command(command, session_guard=guard, timeout_seconds=10)
    return raw.decode("utf-8", errors="replace")


async def sample_once(runtime: Any, collector: Any) -> None:
    """完成一轮结构化采样；不保存原始响应且不会让可选监控中断日志采集。"""
    resource = await _guard(runtime, collector)
    if resource is None:
        return

    async def guard():
        if await _guard(runtime, collector) is None:
            raise ResourceMonitorUnavailable("RESOURCE_MONITOR_LEASE_LOST")

    settings = await runtime.repo.db.platform_settings.find_one(
        {"id": "platform"}, {"resourceMonitor": 1, "version": 1}
    )
    config = parse_monitor_config(settings.get("resourceMonitor") if settings else None)
    version = int(settings.get("version", 1)) if settings else 1
    stamp = now()
    identity = {
        "model": resource.get("model") or "",
        "subSerialNumber": resource.get("subSerialNumber") or "",
    }
    sample: dict[str, Any] = {
        "sampledAt": stamp,
        "status": "FAILED",
        "values": [],
        "configVersion": version,
        "identity": identity,
        "errors": [],
    }
    failed_scopes: set[str] = set()

    def failed(scope: str, error: Exception) -> None:
        if scope in failed_scopes:
            return
        failed_scopes.add(scope)
        code = error.code if isinstance(error, ResourceMonitorUnavailable) else type(error).__name__
        sample["errors"].append({"scope": scope, "code": code})

    try:
        # ensure_ash 内部对 UNKNOWN 先发送 ls；已经确认的 ASH 不会写 debug。
        await collector.ensure_ash_for_monitor(session_guard=guard)
        # 规则各自累计预算，坏规则不能耗尽其它规则的预算；失败项只记录一次。
        process_budgets = {rule["id"]: [0.0] for rule in config["processRules"]}
        values = []
        for item in config["items"]:
            if not item["enabled"]:
                continue
            try:
                output = await _capture(collector, item["command"], guard)
                match = _match(item["pattern"], output, [0.0])
                if match is None:
                    raise ResourceMonitorUnavailable("METRIC_NOT_FOUND")
                values.append(
                    {
                        "id": item["id"],
                        "name": item["name"],
                        "value": _number(match, unit=item["unit"]),
                        "unit": item["unit"],
                    }
                )
            except asyncio.CancelledError:
                raise
            except ResourceMonitorUnavailable as error:
                if error.code == "RESOURCE_MONITOR_LEASE_LOST":
                    raise
                failed(item["id"], error)
            except Exception as error:  # noqa: BLE001 - 单项失败不能终止其余监控。
                failed(item["id"], error)
            sample["values"] = values
        try:
            process_output = await _capture(collector, config["processDiscoveryCommand"], guard)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 进程发现失败保留已采集的系统指标。
            process_output = ""
            failed("process-discovery", error)
        value_pattern = config["processValuePattern"]
        checked = 0
        for line in process_output.splitlines():
            fields = line.strip().split(None, 4)
            if len(fields) < 5 or not fields[0].isdigit():
                continue
            pid, command = int(fields[0]), fields[4]
            if pid <= 0:
                continue
            for rule in config["processRules"]:
                if not rule["enabled"]:
                    continue
                scope = f"process-rule:{rule['id']}"
                if scope in failed_scopes:
                    continue
                try:
                    matched = _match(rule["pattern"], command, process_budgets[rule["id"]])
                except Exception as error:  # noqa: BLE001
                    failed(scope, error)
                    continue
                if matched is None:
                    continue
                name = rule["name"]
                if rule.get("nameGroup"):
                    name = f"{matched.group(rule['nameGroup'])}_{name}"
                checked += 1
                if checked > _MAX_PROCESSES:
                    break
                try:
                    status = await _capture(
                        collector, config["processStatusCommand"].replace("{pid}", str(pid)), guard
                    )
                    rss = _match(value_pattern, status, [0.0])
                    if rss is None:
                        raise ResourceMonitorUnavailable("METRIC_NOT_FOUND")
                    values.append(
                        {
                            "id": f"process:{rule['id']}:{pid}",
                            "name": name,
                            "value": _number(rss, unit="KB"),
                            "unit": "KB",
                            "pid": pid,
                        }
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - 单个进程退出或超时不阻断后续PID。
                    failed(f"process:{pid}", error)
                break
            if checked >= _MAX_PROCESSES:
                break
        sample.update(
            {
                "status": "OK" if not sample["errors"] else ("PARTIAL" if values else "FAILED"),
                "values": values,
            }
        )
    except Exception as error:  # noqa: BLE001 - 可选监控失败只写稳定错误类型，不能把设备正文或命令写入数据库。
        sample["errorCode"] = (
            error.code if isinstance(error, ResourceMonitorUnavailable) else type(error).__name__
        )
    latest = await _guard(runtime, collector)
    if latest is None:
        return
    if sample["identity"] != {
        "model": latest.get("model") or "",
        "subSerialNumber": latest.get("subSerialNumber") or "",
    }:
        return
    await ResourceMetricStore(runtime.repo.db).save(
        runtime.task["resourceId"], sample, retention_days=config["retentionDays"]
    )


async def monitor_loop(runtime: Any, collector: Any) -> None:
    """每分钟采样一次；一轮失败不会阻断采集、命令或下一轮重试。"""
    while (
        not runtime.stopping
        and not runtime.retired
        and runtime.collector is collector
        and not collector._closed.is_set()
    ):
        try:
            await sample_once(runtime, collector)
        except asyncio.CancelledError:
            raise
        except Exception:  # 单轮配置、数据库或租约异常不能终止后续一分钟采样。
            import logging

            logging.getLogger(__name__).exception("资源监控本轮失败 task=%s", runtime.task["id"])
        await asyncio.sleep(60)
