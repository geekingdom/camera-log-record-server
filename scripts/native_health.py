"""原生采集节点健康检查：认证副本集内必须出现当前节点的新鲜心跳。"""

import argparse
import time
from datetime import UTC, datetime, timedelta

from native_config import read_config
from pymongo import MongoClient
from pymongo.errors import PyMongoError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    values = read_config(args.config)
    deadline = time.monotonic() + 90
    with MongoClient(values["MONGO_URI"], serverSelectionTimeoutMS=3000, tz_aware=True) as client:
        while time.monotonic() < deadline:
            try:
                node = client[values["DATABASE_NAME"]].nodes.find_one({"id": values["NODE_ID"]})
                if node and node.get("heartbeat", datetime.min.replace(tzinfo=UTC)) >= datetime.now(UTC) - timedelta(seconds=60):
                    print("节点健康检查通过：数据库心跳新鲜")
                    return 0
            except PyMongoError:
                pass
            time.sleep(1)
    print("节点心跳未就绪，请检查数据库连接及systemd日志")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
