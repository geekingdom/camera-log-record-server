"""经正式 API 和真实 SSH/Telnet 连接测量多路采集，下载小时包比对逐路源摘要。

不修改节点容量、不访问实体设备，不用配置时长冒充持续吞吐证据。输出目录
保存任务身份、每秒节点指标、下载和最终报告；异常也通过正式接口停止合成任务。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from camera_logs.common.config import Settings
from service_benchmark_io import LoadSource, verify_download
from service_benchmark_realtime import observe_realtime
from service_benchmark_search import observe_searches
from service_benchmark_ssh import DeviceInfoSource, SshLoadSource


async def wait_observer_ready(observer, ready, timeout):
    """订阅建立失败立即传播，不能只等 ready 而让设备源静默至重连。"""
    waiting = asyncio.create_task(ready.wait())
    try:
        done, _ = await asyncio.wait({observer, waiting}, timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if observer in done:
            await observer
            raise RuntimeError("实时观察器在开始发送前已退出")
        if waiting not in done:
            raise TimeoutError("等待实时订阅建立超时")
    finally:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


async def emit_observed(source, seconds, timeout, observers):
    """发送与实时校验共同监督；任一观察器失败立即取消并等待当前输出。"""
    async def emit():
        await asyncio.wait_for(source.emit(seconds), timeout=timeout)
        return time.monotonic()

    emitter = asyncio.create_task(emit())
    try:
        end, *observed = await asyncio.gather(emitter, *observers)
        return end, observed
    finally:
        if not emitter.done():
            emitter.cancel()
        await asyncio.gather(emitter, return_exceptions=True)


async def request(client, method, path, **kwargs):
    """检查 HTTP 状态，保留非敏感的路径和状态码，不输出认证头或设备正文。"""
    response = await client.request(method, path, **kwargs)
    response.raise_for_status()
    return response.json()


async def wait_until(client, path, predicate, timeout=90):
    """等待异步任务完成，终态失败立即退出；不把轮询超时当作已停止。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = await request(client, "GET", path)
        if predicate(item):
            return item
        if item.get("status") in {"FAILED", "ERROR", "CANCELLED", "BLOCKED"}:
            raise RuntimeError(f"{path} 状态为 {item['status']}")
        await asyncio.sleep(.1)
    raise TimeoutError(f"等待 {path} 超时")


async def stop(client, task_id, timeout):
    """以幂等停止语义回收连接，并等待操作成功确认。"""
    operation = await request(client, "POST", f"/api/v1/tasks/{task_id}/stop")
    await wait_until(client, f"/api/v1/operations/{operation['id']}",
                     lambda item: item["status"] == "SUCCEEDED", timeout)


async def download(client, task_id, hour_ids, path, timeout):
    """冻结所选小时，限制内存占用流式下载正式产物。"""
    job = await request(client, "POST", "/api/v1/downloads",
        json={"taskId": task_id, "hourIds": hour_ids, "allowPartial": False},
        headers={"Idempotency-Key": uuid4().hex})
    await wait_until(client, f"/api/v1/downloads/{job['id']}",
                     lambda item: item["status"] == "SUCCEEDED", timeout)
    with path.open("xb") as target:
        async with client.stream("GET", f"/api/v1/downloads/{job['id']}/content") as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes(256 * 1024):
                await asyncio.to_thread(target.write, chunk)
    return path.stat().st_size


async def sample_nodes(client, output, finished):
    """按秒记录节点观测值；此处的写入延迟是节点指标，不等同于 API 文件可读延迟。"""
    with (output / "nodes.jsonl").open("x", encoding="utf-8") as target:
        while not finished.is_set():
            nodes = await request(client, "GET", "/api/v1/nodes")
            fields = ("id", "activeTasks", "capacity", "inputBytesPerSecond", "writeLatencyMs",
                      "writeLatencySamples", "writeLatencyPendingMs", "diskPercent", "diskFreeBytes", "accepting")
            snapshot = {"time": datetime.now(UTC).isoformat(),
                        "nodes": [{key: item.get(key) for key in fields} for item in nodes["items"]]}
            target.write(json.dumps(snapshot, ensure_ascii=False) + "\n")
            target.flush()
            try:
                await asyncio.wait_for(finished.wait(), timeout=1)
            except TimeoutError:
                pass


async def execute(args):
    """创建隔离资源并并发运行合成路由，任何失败都回收本轮已创建的任务。"""
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    token = Settings(_env_file=args.env_file).bootstrap_token
    if not token:
        raise ValueError("配置中缺少 BOOTSTRAP_TOKEN")
    sources, task_ids, results, intervals = [], [], [], []
    resource_id, monitor = None, None
    finished = asyncio.Event()
    suffix = uuid4().hex
    cleanup_errors = []
    protocol = getattr(args, "protocol", "TELNET_SERIAL")
    password = uuid4().hex
    device_info = DeviceInfoSource(args.bind_host, password, getattr(args, "device_info_port", 80)) if protocol == "SSH" else None
    downloads = asyncio.Semaphore(args.download_concurrency)
    # 不把整个采集时长放进信号量，确保所有已创建路由能够同时输出。
    creating = asyncio.Semaphore(16)
    workers = []
    observers = []
    search_jobs = set()
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=args.url.rstrip("/"),
        headers={"Authorization": "Bearer " + token}, timeout=120,
        limits=httpx.Limits(max_connections=max(32, args.routes + 8))) as client:
        try:
            node_data = await request(client, "GET", "/api/v1/nodes")
            available = sum(max(0, node.get("capacity", 0) - node.get("activeTasks", 0))
                            for node in node_data["items"] if node.get("accepting"))
            if available < args.routes:
                raise ValueError(f"节点当前可用容量 {available} 小于请求路数 {args.routes}")
            resource_body = {"name": f"协议压测-{suffix}", "kind": "SERIAL_SERVER", "ip": args.device_host}
            if device_info:
                await device_info.start()
                resource_body.update(kind="HIKVISION_NETWORK", username="benchmark", password=password, authType="BASIC")
            resource = await request(client, "POST", "/api/v1/resources",
                json=resource_body,
                headers={"Idempotency-Key": f"benchmark-resource-{suffix}"})
            resource_id = resource["id"]
            (output / "resource.json").write_text(json.dumps({"resourceId": resource_id}), encoding="utf-8")
            monitor = asyncio.create_task(sample_nodes(client, output, finished))

            async def route(number):
                """一条连接独立发送、排空、停止及下载；归档按源摘要验证不混路。"""
                source_args = (number, args.bind_host, args.lines_per_second, args.line_bytes)
                source = SshLoadSource(*source_args, password=password) if protocol == "SSH" else LoadSource(*source_args)
                sources.append(source)
                await source.start()
                async with creating:
                    task = await request(client, "POST", "/api/v1/tasks", json={
                        "name": f"协议压测-{suffix[:8]}-{number:04d}", "resourceId": resource_id,
                        "protocol": protocol, "ip": args.device_host, "port": source.port,
                        **({"username": "benchmark", "password": password} if protocol == "SSH" else {}),
                        "initialCommands": [], "scheduledCommands": [], "autoStart": True},
                        headers={"Idempotency-Key": f"benchmark-task-{suffix}-{number}"})
                    task_id = task["id"]
                    task_ids.append(task_id)
                    with (output / "tasks.jsonl").open("a", encoding="utf-8") as target:
                        target.write(json.dumps({"route": number, "taskId": task_id, "port": source.port}) + "\n")
                await asyncio.wait_for(source.connected.wait(), timeout=args.timeout)
                route_observers = []
                for _ in range(args.realtime_clients_per_route):
                    ready = asyncio.Event()
                    observer = asyncio.create_task(observe_realtime(
                        args.url, token, task_id, number, args.line_bytes,
                        args.seconds * args.lines_per_second, ready, args.seconds * 2 + args.timeout))
                    observers.append(observer)
                    route_observers.append(observer)
                    await wait_observer_ready(observer, ready, args.timeout)
                begin = time.monotonic()
                route_searches = []
                if args.search_interval:
                    search = asyncio.create_task(observe_searches(client, task_id, source,
                        args.search_interval, args.seconds * 2 + args.timeout, search_jobs))
                    observers.append(search)
                    route_searches.append(search)
                source.release.set()
                end, observations = await emit_observed(source, args.seconds, args.seconds * 2 + args.timeout,
                                                        route_observers + route_searches)
                realtime = observations[:len(route_observers)]
                searches = observations[len(route_observers):]
                if args.search_interval and (not searches or searches[0]["count"] < 1):
                    raise AssertionError(f"路由 {number} 未完成任何并发搜索")
                intervals.append((begin, end))
                if source.failure:
                    raise RuntimeError(f"模拟源 {number} 发送失败") from source.failure
                if any(item["sourceSha256"] != source.source_sha256
                       or item["sourceLines"] != source.source_lines for item in realtime):
                    raise AssertionError(f"路由 {number} 实时正文与源日志不一致")
                expected_stored = source.source_bytes + source.source_lines * 22
                hours = await wait_until(client, f"/api/v1/tasks/{task_id}/log-hours",
                    lambda item: sum(hour["bytes"] for hour in item["items"]) >= expected_stored, args.timeout)
                if sum(hour["bytes"] for hour in hours["items"]) != expected_stored:
                    raise AssertionError(f"路由 {number} 登记字节数与源日志不一致")
                await stop(client, task_id, args.timeout)
                await asyncio.wait_for(source.peer_closed.wait(), timeout=args.timeout)
                if protocol == "SSH":
                    await asyncio.wait_for(source.transport_closed.wait(), timeout=args.timeout)
                if source.connection_count != 1:
                    raise AssertionError(f"路由 {number} 发生了重连，不能作为正常连续采集通过")
                hours = await wait_until(client, f"/api/v1/tasks/{task_id}/log-hours",
                    lambda item: bool(item["items"]) and all(h["status"] == "READY" for h in item["items"]), args.timeout)
                async with downloads:
                    paths, size = [], 0
                    for index, hour in enumerate(sorted(hours["items"], key=lambda item: item["hourId"])):
                        path = output / f"route-{number:04d}-hour-{index:03d}.archive"
                        size += await download(client, task_id, [hour["hourId"]], path, args.timeout)
                        paths.append(path)
                    verified = await asyncio.to_thread(verify_download, paths, source.source_sha256, source.source_lines)
                result = {"route": number, "taskId": task_id, "sourceLines": source.source_lines,
                    "sourceBytes": source.source_bytes, "sourceSha256": source.source_sha256,
                    "elapsedSeconds": source.elapsed_seconds, "maxTickLagSeconds": source.max_tick_lag_seconds,
                    "connectionCount": source.connection_count, "downloadBytes": size,
                    "realtime": realtime, "searches": searches, "verification": verified}
                results.append(result)
                print(json.dumps({"route": number, "verified": True, "completedRoutes": len(results)}), flush=True)

            workers = [asyncio.create_task(route(number)) for number in range(args.routes)]
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                if not worker.done():
                    worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)
            for observer in observers:
                if not observer.done():
                    observer.cancel()
            await asyncio.gather(*observers, return_exceptions=True)
            for identifier in sorted(search_jobs):
                try:
                    response = await client.delete(f"/api/v1/log-searches/{identifier}")
                    response.raise_for_status()
                    await wait_until(client, f"/api/v1/log-searches/{identifier}",
                        lambda item: item.get("status") in {"SUCCEEDED", "FAILED", "CANCELLED"}, args.timeout)
                except Exception as error:  # noqa: BLE001 - 搜索清理失败不能阻止采集连接回收。
                    cleanup_errors.append({"searchId": identifier, "errorType": type(error).__name__})
            # 先向服务请求停止，再关闭模拟端，避免关闭监听触发不必要的重连。
            async def cleanup(task_id):
                try:
                    async with creating:
                        await stop(client, task_id, args.timeout)
                except Exception as error:  # noqa: BLE001 - 记录清理失败后继续回收其他独立任务。
                    cleanup_errors.append({"taskId": task_id, "errorType": type(error).__name__})
            await asyncio.gather(*(cleanup(task_id) for task_id in task_ids))
            closed = await asyncio.gather(*(source.close() for source in sources), return_exceptions=True)
            for index, outcome in enumerate(closed):
                if isinstance(outcome, BaseException):
                    cleanup_errors.append({"source": index, "closeError": type(outcome).__name__})
            if device_info:
                try:
                    await device_info.close()
                except Exception as error:  # noqa: BLE001 - 模拟端清理失败不能跳过资源软删除。
                    cleanup_errors.append({"deviceInfoError": type(error).__name__})
            if resource_id:
                try:
                    resource = await request(client, "GET", f"/api/v1/resources/{resource_id}")
                    await request(client, "DELETE", f"/api/v1/resources/{resource_id}?version={resource['version']}")
                    await wait_until(client, f"/api/v1/resources/{resource_id}",
                        lambda item: item.get("deletionState") == "DONE" and item.get("activeTaskCount") == 0,
                        args.timeout)
                except Exception as error:  # noqa: BLE001 - 将资源清理失败计入最终验收结果。
                    cleanup_errors.append({"resourceId": resource_id, "errorType": type(error).__name__})
            finished.set()
            if monitor:
                observation = (await asyncio.gather(monitor, return_exceptions=True))[0]
                if isinstance(observation, BaseException):
                    cleanup_errors.append({"metricsError": type(observation).__name__})
            (output / "cleanup.json").write_text(json.dumps({"errors": cleanup_errors}, indent=2), encoding="utf-8")
    overlap = max(0, min(end for _, end in intervals) - max(begin for begin, _ in intervals))
    report = {"scope": f"real-{'ssh' if protocol == 'SSH' else 'telnet'}-api-worker-mongo-download", "protocol": protocol, "routes": args.routes,
        "configuredSeconds": args.seconds, "linesPerSecondPerRoute": args.lines_per_second,
        "lineBytes": args.line_bytes, "allRoutesOverlapSeconds": round(overlap, 3),
        "totalElapsedSeconds": round(time.monotonic() - started, 3),
        "integrityVerified": len(results) == args.routes,
        "targetRateAchieved": all(item["elapsedSeconds"] <= args.seconds * 1.05 for item in results),
        "concurrentWindowVerified": overlap >= args.seconds * .95,
        "fullDayConcurrencyVerified": overlap >= 86400,
        "sourceLines": sum(item["sourceLines"] for item in results),
        "sourceBytes": sum(item["sourceBytes"] for item in results),
        "cleanupVerified": not cleanup_errors, "results": sorted(results, key=lambda item: item["route"])}
    report["realtimeClientsPerRoute"] = args.realtime_clients_per_route
    report["realtimeFrames"] = sum(observer["frames"] for item in results for observer in item["realtime"])
    report["realtimeLogBytes"] = sum(observer["logBytes"] for item in results for observer in item["realtime"])
    report["realtimeVerified"] = all(len(item["realtime"]) == args.realtime_clients_per_route
                                     for item in results) and len(results) == args.routes
    report["searchIntervalSeconds"] = args.search_interval
    report["searchCount"] = sum(search["count"] for item in results for search in item["searches"])
    report["searchMaxElapsedSeconds"] = max((search["maxElapsedSeconds"] for item in results
        for search in item["searches"]), default=0)
    report["searchVerified"] = not args.search_interval or (len(results) == args.routes and all(
        len(item["searches"]) == 1 and item["searches"][0]["count"] > 0 for item in results))
    report["passed"] = all(report[key] for key in (
        "integrityVerified", "targetRateAchieved", "concurrentWindowVerified", "cleanupVerified",
        "realtimeVerified", "searchVerified"))
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def parse_args():
    """默认少量真实协议任务；大规模验收需显式给定路数、时长和独立输出目录。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5173")
    parser.add_argument("--device-host", default="127.0.0.1")
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument("--protocol", choices=["TELNET_SERIAL", "SSH"], default="TELNET_SERIAL")
    parser.add_argument("--device-info-port", type=int, default=80,
                        help="SSH 模拟 ISAPI 监听端口；非 80 时须由测试环境转发设备地址的 80 端口")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--routes", type=int, default=8)
    parser.add_argument("--seconds", type=int, default=36)
    parser.add_argument("--lines-per-second", type=int, default=1200)
    parser.add_argument("--line-bytes", type=int, default=256)
    parser.add_argument("--download-concurrency", type=int, default=2)
    parser.add_argument("--realtime-clients-per-route", type=int, default=0)
    parser.add_argument("--search-interval", type=int, default=0)
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.device_info_port <= 65535:
        parser.error("设备信息监听端口须在 1..65535")
    if not 1 <= args.routes <= 500 or not 1 <= args.seconds <= 172800:
        parser.error("路数须在 1..500，秒数须在 1..172800")
    if not 1 <= args.lines_per_second <= 1200 or not 64 <= args.line_bytes <= 65536:
        parser.error("每秒行数须在 1..1200，每行字节须在 64..65536")
    if not 1 <= args.download_concurrency <= 8 or args.timeout <= 0:
        parser.error("下载并发须在 1..8，超时须为正整数")
    if not 0 <= args.realtime_clients_per_route <= 4:
        parser.error("每路实时订阅数须在 0..4，0 表示不订阅")
    if args.search_interval < 0:
        parser.error("搜索间隔须为非负整数秒，0 表示关闭")
    return args


if __name__ == "__main__":
    options = parse_args()
    try:
        result = asyncio.run(execute(options))
        print(json.dumps({key: value for key, value in result.items() if key != "results"}, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result["passed"] else 2)
    except Exception as error:  # noqa: BLE001 - CLI 统一以非零退出码报告失败。
        print(json.dumps({"passed": False, "errorType": type(error).__name__, "message": str(error)}, ensure_ascii=False))
        raise SystemExit(2)
