"""测量本地 Telnet 输入到正式日志读取 API 首次返回完整行的延迟上界。

该结果包含源端发送、网络、节点写入、API 代理与 10ms 轮询，不能代表节点内部
写入延迟。默认十秒且最长二十秒，避免本工具进入 10 MiB 轮转场景。
"""

import argparse
import asyncio
import base64
import hashlib
import json
import math
import time
from pathlib import Path
from uuid import uuid4

import httpx
from benchmark_service import download, request, stop, wait_until
from camera_logs.common.config import Settings
from service_benchmark_io import PREFIX, LoadSource, verify_download


def percentile(values, fraction):
    """返回最近秩百分位，空集合在报告前由调用方拒绝。"""
    ordered = sorted(values)
    return ordered[math.ceil(len(ordered) * fraction) - 1]


def complete_lines(pending, data, width):
    """仅接受固定宽度和时间前缀的完整行，返回可流式摘要的原始设备正文。"""
    pieces = (pending + data).split(b"\n")
    tail = pieces.pop()
    bodies = []
    for item in pieces:
        line = item + b"\n"
        if len(line) != width or PREFIX.match(item) is None:
            raise AssertionError("日志读取返回了缺少前缀或长度不符的完整行")
        bodies.append(item[22:] + b"\n")
    return tail, bodies


async def first_file(client, task_id, timeout):
    """等待运行中的小时目录登记首个可读文件 ID，不访问数据库或节点内部接口。"""
    hours = await wait_until(client, f"/api/v1/tasks/{task_id}/log-hours",
                             lambda item: any(hour.get("files") for hour in item["items"]), timeout)
    files = [file for hour in hours["items"] for file in hour["files"]]
    return files[0]["id"]


async def discover_files(client, task_id, seen):
    """按小时升序和目录内文件顺序发现后继文件，已读取或已入队的文件不得重复。"""
    hours = await request(client, "GET", f"/api/v1/tasks/{task_id}/log-hours")
    found = []
    for hour in sorted(hours["items"], key=lambda item: item["hour"]):
        for file in hour["files"]:
            if file["id"] not in seen:
                seen.add(file["id"])
                found.append(file["id"])
    return found


def settle_batches(batches, done, lines, completed_at, samples):
    """以最近一次内容完整化时刻结算已满足的批次，兼容晚于读取才到达的回调。"""
    if completed_at is None:
        return done
    while done < len(batches) and lines >= batches[done]["after"]:
        batch = batches[done]
        batch["readAtMonotonic"] = completed_at
        batch["latencySeconds"] = completed_at - batch["writeStartedMonotonic"]
        samples.append(batch["latencySeconds"])
        done += 1
    return done


def verify_content(observed, expected_sha256, expected_lines):
    """确认实际内容 API 的已读正文与源端摘要和行数相同，下载归档不能替代该检查。"""
    if observed["sha256"] != expected_sha256 or observed["lines"] != expected_lines:
        raise AssertionError("正式内容 API 返回的正文摘要或行数与源端不一致")


async def observe(client, task_id, file_id, batches, emitter, line_width, timeout):
    """轮询内容 API 并跨小时续读后继文件，返回完整行延迟与真实 API 正文摘要。"""
    deadline, offset, pending, lines, done = time.monotonic() + timeout, 0, b"", 0, 0
    current, queued, seen, next_catalog = file_id, [], {file_id}, 0.0
    samples, digest, completed_at, read_files = [], hashlib.sha256(), None, [file_id]
    while time.monotonic() < deadline:
        response = await client.get(f"/api/v1/log-files/{current}/content", params={"offset": offset, "limit": 262144})
        response.raise_for_status()
        body = response.json()
        data, next_offset = base64.b64decode(body["data"]), body["nextOffset"]
        if next_offset != offset + len(data):
            raise AssertionError("内容 API 返回的 nextOffset 与实际字节数不连续")
        offset = next_offset
        if data:
            pending, bodies = complete_lines(pending, data, line_width)
            for source in bodies:
                digest.update(source)
            lines += len(bodies)
            completed_at = time.monotonic()
        done = settle_batches(batches, done, lines, completed_at, samples)
        moment = time.monotonic()
        if moment >= next_catalog:
            queued.extend(await discover_files(client, task_id, seen))
            next_catalog = moment + .1
        if emitter.done() and done == len(batches):
            await emitter
            return {"samples": samples, "sha256": digest.hexdigest(), "lines": lines, "fileIds": read_files}
        if not data and queued:
            current, offset = queued.pop(0), 0
            read_files.append(current)
            continue
        await asyncio.sleep(.01)
    raise TimeoutError("日志内容 API 未在限定时间内返回全部批次的完整行")


async def execute(args):
    """创建独立资源任务、输出受控时间的源日志，读取测量后下载核验并完成软删除。"""
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    token = Settings(_env_file=args.env_file).bootstrap_token
    if not token:
        raise ValueError("配置中缺少 BOOTSTRAP_TOKEN")
    batches, cleanup_errors, task_id, resource_id, source, report = [], [], None, None, None, None
    suffix = uuid4().hex
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=args.url.rstrip("/"), headers={"Authorization": "Bearer " + token}, timeout=args.timeout) as client:
        try:
            nodes = await request(client, "GET", "/api/v1/nodes")
            available = sum(max(0, node.get("capacity", 0) - node.get("activeTasks", 0))
                            for node in nodes["items"] if node.get("accepting"))
            if available < 1:
                raise RuntimeError("没有可用节点执行读延迟基线")
            resource = await request(client, "POST", "/api/v1/resources", json={
                "name": f"读延迟压测-{suffix}", "kind": "SERIAL_SERVER", "ip": args.device_host},
                headers={"Idempotency-Key": f"read-latency-resource-{suffix}"})
            resource_id = resource["id"]
            def on_batch(write_started, before, after):
                """记录固定时长内至多二百批的源端发送边界，避免压测元数据无界增长。"""
                batches.append({"writeStartedMonotonic": write_started, "before": before, "after": after})
            source = LoadSource(0, args.bind_host, args.lines_per_second, args.line_bytes, on_batch=on_batch)
            await source.start()
            task = await request(client, "POST", "/api/v1/tasks", json={
                "name": f"读延迟压测-{suffix[:8]}", "resourceId": resource_id, "protocol": "TELNET_SERIAL",
                "ip": args.device_host, "port": source.port, "initialCommands": [], "scheduledCommands": [], "autoStart": True},
                headers={"Idempotency-Key": f"read-latency-task-{suffix}"})
            task_id = task["id"]
            await asyncio.wait_for(source.connected.wait(), args.timeout)
            source.release.set()
            emitter = asyncio.create_task(source.emit(args.seconds))
            file_id = await first_file(client, task_id, args.timeout)
            observed = await observe(client, task_id, file_id, batches, emitter, args.line_bytes + 22, args.timeout)
            samples = observed["samples"]
            if source.failure or source.connection_count != 1:
                raise RuntimeError("模拟源连接或发送失败") from source.failure
            verify_content(observed, source.source_sha256, source.source_lines)
            await stop(client, task_id, args.timeout)
            await asyncio.wait_for(source.peer_closed.wait(), args.timeout)
            hours = await wait_until(client, f"/api/v1/tasks/{task_id}/log-hours",
                lambda item: bool(item["items"]) and all(hour["status"] == "READY" for hour in item["items"]), args.timeout)
            archive = output / "archive.download"
            await download(client, task_id, [hour["hourId"] for hour in hours["items"]], archive, args.timeout)
            integrity = await asyncio.to_thread(verify_download, archive, source.source_sha256, source.source_lines)
            report = {"scope": "real-telnet-to-formal-log-content-api", "configuredSeconds": args.seconds,
                "linesPerSecond": args.lines_per_second, "lineBytes": args.line_bytes, "fileId": file_id,
                "batchCount": len(samples), "p50Seconds": percentile(samples, .50), "p99Seconds": percentile(samples, .99),
                "maxSeconds": max(samples), "sourceLines": source.source_lines, "sourceBytes": source.source_bytes,
                "sourceElapsedSeconds": source.elapsed_seconds, "contentRead": observed, "integrity": integrity,
                "interpretation": "source-send 到 API 已读完整行，含网络、节点写入、API 代理和 10ms 轮询的上界，不是节点内部指标",
                "batches": batches}
        finally:
            if task_id:
                try:
                    await stop(client, task_id, args.timeout)
                except Exception as error:  # noqa: BLE001 - 清理其余资源仍必须继续。
                    cleanup_errors.append({"taskId": task_id, "errorType": type(error).__name__})
            if source:
                result = (await asyncio.gather(source.close(), return_exceptions=True))[0]
                if isinstance(result, BaseException):
                    cleanup_errors.append({"source": "close", "errorType": type(result).__name__})
            if resource_id:
                try:
                    resource = await request(client, "GET", f"/api/v1/resources/{resource_id}")
                    await request(client, "DELETE", f"/api/v1/resources/{resource_id}?version={resource['version']}")
                    await wait_until(client, f"/api/v1/resources/{resource_id}",
                                     lambda item: item.get("deletionState") == "DONE", args.timeout)
                except Exception as error:  # noqa: BLE001 - 报告资源软删除失败而非静默遗留。
                    cleanup_errors.append({"resourceId": resource_id, "errorType": type(error).__name__})
            (output / "cleanup.json").write_text(json.dumps({"errors": cleanup_errors}, indent=2), encoding="utf-8")
    if report is None:
        raise RuntimeError("读延迟基线未生成报告")
    report["cleanupVerified"] = not cleanup_errors
    report["totalElapsedSeconds"] = time.monotonic() - started
    report["sloP99Under200ms"] = report["p99Seconds"] <= .2
    report["passed"] = (report["cleanupVerified"] and report["batchCount"] == args.seconds * 10
                        and report["sloP99Under200ms"])
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def parse_args():
    """限制为短时单路基线，避免将读延迟工具误用于长时间或 10 MiB 轮转压测。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5173")
    parser.add_argument("--device-host", default="127.0.0.1")
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--lines-per-second", type=int, default=1200)
    parser.add_argument("--line-bytes", type=int, default=256)
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--output", type=Path, default=Path(".local/read-latency-before"))
    args = parser.parse_args()
    if not 1 <= args.seconds <= 20 or not 1 <= args.lines_per_second <= 1200 or not 64 <= args.line_bytes <= 65536 or args.timeout <= 0:
        parser.error("时长须为 1..20 秒，每秒行数须为 1..1200，每行字节须为 64..65536")
    if args.seconds * args.lines_per_second * (args.line_bytes + 22) >= 10 * 1024 * 1024:
        parser.error("本工具输入不得达到 10 MiB 轮转阈值")
    return args


if __name__ == "__main__":
    options = parse_args()
    try:
        outcome = asyncio.run(execute(options))
        print(json.dumps({key: value for key, value in outcome.items() if key != "batches"}, ensure_ascii=False))
        raise SystemExit(0 if outcome["passed"] else 2)
    except Exception as error:  # noqa: BLE001 - CLI 以非零退出码报告正式基线失败。
        print(json.dumps({"passed": False, "errorType": type(error).__name__, "message": str(error)}, ensure_ascii=False))
        raise SystemExit(2)
