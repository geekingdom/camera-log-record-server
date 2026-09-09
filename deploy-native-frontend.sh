#!/usr/bin/env bash
# 独立部署前端：先 --init 配置BACKEND_UPSTREAM，再部署；无需MongoDB或Python应用依赖。
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$root/deploy-native.sh" frontend "$@"
