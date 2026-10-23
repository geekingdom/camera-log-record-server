"""通过实际资源指标日期路由验证上海日期边界与 Mongo 复合索引命中。"""

import argparse
import asyncio
import ipaddress
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from camera_logs.resource_metrics.api import install_resource_metric_routes
from fastapi import FastAPI
from pymongo import AsyncMongoClient


def _loopback_uri(value: str) -> str:
    """只接受数值回环 Mongo URI，防止验证脚本误连到共享或生产数据库。"""
    parsed = urlsplit(value)
    if parsed.scheme != "mongodb" or not parsed.hostname:
        raise ValueError("URI必须是 mongodb://127.0.0.1 或 mongodb://[::1] 形式")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as error:
        raise ValueError("URI主机必须是数值回环地址，不能使用主机名") from error
    if not address.is_loopback:
        raise ValueError("URI主机必须是回环地址")
    return value


def _first_ixscan(value: object) -> dict | None:
    """从 explain 胜出计划中定位实际 IXSCAN 节点，兼容 FETCH/PROJECTION 包裹。"""
    if isinstance(value, dict):
        if value.get("stage") == "IXSCAN":
            return value
        for child in value.values():
            found = _first_ixscan(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _first_ixscan(child)
            if found is not None:
                return found
    return None


class RecordingMetricHours:
    """保留真实异步 collection 行为，仅记录实际路由提交的聚合管道以供 Explain 复用。"""

    def __init__(self, collection):
        self.collection = collection
        self.pipelines: list[list[dict]] = []

    async def aggregate(self, pipeline, *args, **kwargs):
        self.pipelines.append(pipeline)
        return await self.collection.aggregate(pipeline, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.collection, name)


class DatabaseProxy:
    """把日期路由的指标集合接入记录代理，其余集合仍使用真实异步 Mongo。"""

    def __init__(self, database):
        self.database = database
        self.resource_metric_hours = RecordingMetricHours(database.resource_metric_hours)

    def __getattr__(self, name):
        return getattr(self.database, name)


def _date_endpoint():
    """注册生产路由并取得其端点，验证脚本不复制生产聚合实现。"""
    app = FastAPI()
    install_resource_metric_routes(app)
    return next(route.endpoint for route in app.routes if route.path == "/api/v1/resources/{resource_id}/resource-metric-dates")


async def verify(uri: str) -> dict:
    """调用实际日期路由检查跨月、空桶和到期桶，最后无条件删除随机验证库。"""
    client = AsyncMongoClient(_loopback_uri(uri), serverSelectionTimeoutMS=5000, tz_aware=True)
    database_name = f"verify_resource_metric_dates_{uuid4().hex}"
    try:
        database = client[database_name]
        collection = database.resource_metric_hours
        await collection.create_index([("resourceId", 1), ("bucketStart", -1)])
        stamp = datetime.now(UTC)
        resource_id = "soft-deleted-resource"
        await database.resources.insert_one({"id": resource_id, "deletedAt": stamp})
        await collection.insert_many([
            {"resourceId": resource_id, "bucketStart": datetime(2026, 10, 31, 15, tzinfo=UTC),
             "expiresAt": stamp + timedelta(days=2),
             "samples": [{"sampledAt": datetime(2026, 10, 31, 15, 59, tzinfo=UTC)}]},
            {"resourceId": resource_id, "bucketStart": datetime(2026, 10, 31, 16, tzinfo=UTC),
             "expiresAt": stamp + timedelta(days=2),
             "samples": [{"sampledAt": datetime(2026, 10, 31, 16, tzinfo=UTC)}]},
            {"resourceId": resource_id, "bucketStart": datetime(2026, 10, 20, 1, tzinfo=UTC),
             "expiresAt": stamp + timedelta(days=2), "samples": []},
            {"resourceId": resource_id, "bucketStart": datetime(2026, 10, 20, 2, tzinfo=UTC),
             "expiresAt": stamp - timedelta(seconds=1),
             "samples": [{"sampledAt": datetime(2026, 10, 20, 2, tzinfo=UTC)}]},
        ])
        proxy = DatabaseProxy(database)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(repo=SimpleNamespace(db=proxy))))
        endpoint = _date_endpoint()
        user = {"scopes": ["*"]}
        october = (await endpoint(resource_id, request, user, "2026-10"))["days"]
        november = (await endpoint(resource_id, request, user, "2026-11"))["days"]
        if october != ["2026-10-31"] or november != ["2026-11-01"]:
            raise AssertionError({"october": october, "november": november})
        pipeline = proxy.resource_metric_hours.pipelines[0]
        explain = await database.command(
            "explain", {"aggregate": collection.name, "pipeline": pipeline, "cursor": {}}, verbosity="executionStats"
        )
        cursor = explain["stages"][0]["$cursor"]
        ixscan = _first_ixscan(cursor["queryPlanner"]["winningPlan"])
        stats = cursor["executionStats"]
        if ixscan is None or ixscan.get("indexName") != "resourceId_1_bucketStart_-1":
            raise AssertionError("资源指标日期聚合未命中 resourceId_1_bucketStart_-1 索引")
        return {
            "octoberDays": october,
            "novemberDays": november,
            "explain": {
                "stage": ixscan["stage"], "index": ixscan["indexName"],
                "keysExamined": stats["totalKeysExamined"], "docsExamined": stats["totalDocsExamined"],
            },
        }
    finally:
        await client.drop_database(database_name)
        await client.close()


def main() -> None:
    """解析本机 URI 并打印可归档的非敏感验证摘要。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--uri", default="mongodb://127.0.0.1:27119/?replicaSet=metricCalendar",
        help="仅允许数值回环地址的 MongoDB URI，默认使用本机验证副本集。",
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(verify(args.uri)), ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
