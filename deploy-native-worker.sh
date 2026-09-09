#!/usr/bin/env bash
# 独立部署采集节点：填写与平台一致的数据库、加密密钥和内部令牌，设置唯一NODE_ID。
# LOG_ROOT是设备日志保存目录；修改前先停止节点并迁移日志，不会自动清理原目录。
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$root/deploy-native.sh" worker "$@"
