"""低峰补齐旧运行事件 ``createdAt``；默认预览，--apply 才实际写入。"""

import argparse
import asyncio
import json

from camera_logs.common.config import Settings
from camera_logs.common.runtime_event_time import backfill_runtime_event_created_at
from pymongo import AsyncMongoClient


async def run(apply: bool, max_records: int = 5000) -> dict:
    """使用当前环境数据库连接执行一个有界回填批次，并始终关闭客户端。"""
    settings = Settings()
    client = AsyncMongoClient(settings.mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        return await backfill_runtime_event_created_at(
            client[settings.database_name], apply=apply, max_records=max_records,
        )
    finally:
        await client.close()


def parse_args() -> argparse.Namespace:
    """解析显式写入开关和受限批次上限，避免误触无限迁移。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="实际写入；默认仅预览")
    parser.add_argument("--max-records", type=int, default=5000,
                        help="单次处理上限，默认5000，最大100000；达到上限后可再次执行")
    arguments = parser.parse_args()
    if not 1 <= arguments.max_records <= 100_000:
        parser.error("--max-records 必须在1至100000之间")
    return arguments


if __name__ == "__main__":
    args = parse_args()
    print(json.dumps(asyncio.run(run(args.apply, args.max_records)), ensure_ascii=False, sort_keys=True))
