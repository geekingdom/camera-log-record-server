#!/usr/bin/env bash
# 独立部署API：先 --init 填写MONGO_URI，配置平台密钥及可信代理来源IP。
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$root/deploy-native.sh" backend "$@"
