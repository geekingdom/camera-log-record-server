#!/usr/bin/env bash
# 部署完整平台（前端、后端、采集节点、三成员数据库）。服务运行需要 Linux 与 Docker Compose。
# 先执行本脚本 --init 生成配置，或 --help 查看通用参数；完整部署可直接执行。
# 可自定义：HOST_LOG_ROOT、API_DATA_ROOT、FRONTEND_PORT、RETENTION_DAYS。完整中文说明见 docs/deployment.md。
# 配置文件：.env；可用 --env-file /绝对路径/部署.env 指定其它配置。
# 已有配置、密钥和存储均保留；脚本不执行删除卷或清空日志操作。
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$root/deploy.sh" --component all "$@"
