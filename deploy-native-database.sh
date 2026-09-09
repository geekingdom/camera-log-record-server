#!/usr/bin/env bash
# 独立部署MongoDB认证单成员副本集：支持事务，但单机不提供硬件故障高可用。
# 跨机访问必须修改MONGO_BIND_IP、MONGO_ADVERTISED_HOST与客户端MONGO_URI。
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$root/deploy-native.sh" database "$@"
