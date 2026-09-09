#!/bin/sh
# 为 host 网络 MongoDB 成员在隔离网络中预置管理员，避免官方入口临时占用宿主机 27017。
set -eu

started=0

cleanup() {
    # mongod 已启动时总在退出前关闭，避免一次性容器残留临时数据库进程。
    if [ "$started" -eq 1 ]; then
        mongod --dbpath /data/db --shutdown >/dev/null 2>&1 || true
    fi
}

trap cleanup EXIT HUP INT TERM

# 非空数据目录可能正被已部署的 host 网络成员使用。保留既有认证和拓扑，绝不尝试取得锁。
if find /data/db -mindepth 1 -maxdepth 1 -print -quit | grep -q .; then
    echo "数据库数据目录已存在，跳过管理员预初始化"
    exit 0
fi

chown mongodb:mongodb /data/db
gosu mongodb mongod --dbpath /data/db --bind_ip 127.0.0.1 --port 27017 \
    --logpath /tmp/mongo-host-user-init.log --fork
started=1

# 使用进程环境读取凭据，避免把密码拼入命令行或脚本输出。
attempt=0
until mongosh --quiet --host 127.0.0.1 --port 27017 \
    --eval 'quit(db.adminCommand({ping: 1}).ok ? 0 : 1)'; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
        echo "MongoDB 管理员预初始化超时" >&2
        exit 1
    fi
    sleep 1
done

mongosh --quiet --host 127.0.0.1 --port 27017 --eval '
const admin = db.getSiblingDB("admin");
if (!admin.getUser(process.env.MONGO_INITDB_ROOT_USERNAME)) {
  admin.createUser({
    user: process.env.MONGO_INITDB_ROOT_USERNAME,
    pwd: process.env.MONGO_INITDB_ROOT_PASSWORD,
    roles: ["root"],
  });
}
'
