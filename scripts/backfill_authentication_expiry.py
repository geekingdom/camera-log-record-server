"""升级旧认证记录的90天保留策略；默认预览，--apply 才写入绝对过期时间。"""

import argparse
import asyncio
import json

from camera_logs.common.authentication_retention import backfill_authentication_expiry
from camera_logs.common.config import Settings
from pymongo import AsyncMongoClient


async def run(apply: bool, max_records: int = 5000) -> dict:
    """读取当前环境数据库设置，关闭连接前完成有界批次迁移。"""
    settings = Settings()
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        return await backfill_authentication_expiry(
            client[settings.database_name], settings.authentication_record_retention_days,
            apply=apply, max_records=max_records)
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="补齐旧认证历史 TTL；过期记录随后由 Mongo 自动清理")
    parser.add_argument("--apply", action="store_true", help="实际写入；不提供时仅预览条数")
    parser.add_argument("--max-records", type=int, default=5000,
                        help="单次处理上限，默认5000，最大100000；达到上限后可再次执行")
    arguments = parser.parse_args()
    print(json.dumps(asyncio.run(run(arguments.apply, arguments.max_records)), ensure_ascii=False))
