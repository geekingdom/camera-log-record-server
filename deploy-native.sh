#!/usr/bin/env bash
# Ubuntu/Debian 无Docker主机部署。默认完整部署，支持单独选择组件。
# 首次可先 --init，再修改 --config 文件中的日志路径、端口和网络地址。
# 例：sudo bash deploy-native.sh all --config /etc/camera-logs/native.env
# 只读取配置，不source；不会执行配置文本，不会删除已有数据库或设备日志。
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! command -v python3 >/dev/null 2>&1; then
  if [[ "$EUID" -eq 0 ]] && command -v apt-get >/dev/null 2>&1; then
    apt-get update
    apt-get install -y python3
  else
    echo "请使用sudo在Ubuntu/Debian上运行以安装python3" >&2
    exit 1
  fi
fi
exec python3 "$root/scripts/deploy_native.py" "$@"
