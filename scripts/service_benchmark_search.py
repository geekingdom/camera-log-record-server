"""服务压测期间通过正式搜索 API 抽查刚写入日志的可检索性。"""

import asyncio
import time
from uuid import uuid4


class SearchFailed(RuntimeError):
    """服务已明确返回失败终态，调用方无需再取消该作业。"""


async def _request(client, method, path, **kwargs):
    """执行正式 API 请求，状态码异常直接交给调用方统一失败处理。"""
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


async def _wait_search(client, identifier, deadline):
    """等待搜索作业到达终态；超时不改变作业状态，留给调用方清理。"""
    while True:
        job = await _request(client, "GET", f"/api/v1/log-searches/{identifier}")
        status = job.get("status")
        if status == "SUCCEEDED":
            return job
        if status in {"FAILED", "CANCELLED", "BLOCKED", "ERROR"}:
            raise SearchFailed(f"搜索作业 {identifier} 状态为 {status}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"搜索作业 {identifier} 超时")
        await asyncio.sleep(min(.1, remaining))


def _keyword(source, stored_bytes):
    """选择靠近已登记水位的倒数第二条，避免搜索仍在写入中的末尾半行。"""
    stored_lines = stored_bytes // (source.line_bytes + 22)
    sequence = max(0, min(source.source_lines, stored_lines) - 2)
    return f"route={source.route:04d} seq={sequence:09d} "


async def _search_once(client, task_id, source, deadline, created_jobs):
    """在已有完整日志水位时创建、等待并验证一次搜索作业。"""
    hours = await _request(client, "GET", f"/api/v1/tasks/{task_id}/log-hours")
    stored_bytes = sum(item.get("bytes", 0) for item in hours.get("items", []))
    if source.source_lines <= 0 or stored_bytes < source.line_bytes + 22:
        return None
    keyword = _keyword(source, stored_bytes)
    created = await _request(
        client,
        "POST",
        "/api/v1/log-searches",
        json={"taskId": task_id, "keyword": keyword},
        headers={"Idempotency-Key": uuid4().hex},
    )
    identifier = created["id"]
    created_jobs.add(identifier)
    started = time.monotonic()
    try:
        job = await _wait_search(client, identifier, deadline)
    except SearchFailed:
        created_jobs.discard(identifier)
        raise
    try:
        results = await _request(client, "GET", f"/api/v1/log-searches/{identifier}/results", params={"pageSize": 100})
        frozen_ids = {item.get("id") for item in job.get("files", [])}
        items = results.get("items")
        if (
            results.get("status") != "SUCCEEDED"
            or results.get("truncated")
            or results.get("total") != 1
            or not isinstance(items, list)
            or len(items) != 1
        ):
            raise AssertionError("搜索作业未返回唯一且未截断的匹配结果")
        matched = items[0]
        if (
            not isinstance(matched.get("text"), str)
            or keyword not in matched["text"]
            or type(matched.get("offset")) is not int
            or matched["offset"] < 0
            or matched.get("fileId") not in frozen_ids
        ):
            raise AssertionError("搜索结果与冻结日志或目标关键字不一致")
        return time.monotonic() - started
    finally:
        # 已确认 SUCCEEDED 后不再需要取消，即使结果校验本身失败也不能无限保留 ID。
        created_jobs.discard(identifier)


async def observe_searches(client, task_id, source, interval, timeout, created_jobs):
    """源输出期间串行搜索，返回成功次数与单次作业最大耗时。

    ``created_jobs`` 必须是调用方清理的 ``set[str]``：作业创建后立刻加入，终态
    后移除；超时或请求异常时保持其中，以便调用方取消仍可能运行的作业。外部取消
    不捕获，避免掩盖任务清理链路。
    """
    if interval <= 0 or timeout <= 0:
        raise ValueError("搜索间隔和超时必须为正数")
    deadline = time.monotonic() + timeout
    count = 0
    max_elapsed = 0.0
    while not source.finished.is_set():
        if time.monotonic() >= deadline:
            raise TimeoutError("等待可搜索日志超时")
        elapsed = await _search_once(client, task_id, source, deadline, created_jobs)
        if elapsed is not None:
            count += 1
            max_elapsed = max(max_elapsed, elapsed)
        try:
            await asyncio.wait_for(source.finished.wait(), timeout=interval)
        except TimeoutError:
            pass
    return {"count": count, "maxElapsedSeconds": max_elapsed}
