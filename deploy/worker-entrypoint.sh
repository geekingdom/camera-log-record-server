#!/bin/sh
set -eu

# 未提供 NODE_ID 时使用容器 hostname，保证扩容副本拥有不同节点标识。
export NODE_ID="${NODE_ID:-$(hostname)}"
# 默认公布容器内部可路由地址，生产多主机部署应显式覆盖 NODE_URL。
export NODE_PORT="${NODE_PORT:-18081}"
export NODE_URL="${NODE_URL:-http://$(hostname -i | awk '{print $1}'):${NODE_PORT}}"
# 统一本地日志根目录，并在启动 worker 前确保目录存在。
export LOG_ROOT="${LOG_ROOT:-/var/lib/camera-logs}"
mkdir -p "$LOG_ROOT"
exec python -m camera_logs.worker
