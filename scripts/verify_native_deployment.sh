#!/usr/bin/env bash
# 在 Ubuntu/Debian 临时主机验证无 Docker 的完整部署；只清理由本脚本创建的临时安装根。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
timeout=300
if [[ "${1:-}" == "--timeout" && -n "${2:-}" ]]; then
  timeout="$2"
  shift 2
fi
if [[ $# -ne 0 ]]; then
  echo "用法：scripts/verify_native_deployment.sh [--timeout 秒]" >&2
  exit 2
fi
if [[ "$(uname -s)" != Linux ]] || [[ ! -r /etc/os-release ]] || ! grep -Eq '^ID=(ubuntu|debian)$' /etc/os-release; then
  echo "原生部署验证仅支持 Ubuntu 或 Debian Linux" >&2
  exit 1
fi
if [[ $EUID -ne 0 ]]; then
  exec sudo --preserve-env=PATH bash "$0" --timeout "$timeout"
fi

temporary="$(mktemp -d /opt/camera-native-smoke-XXXXXXXX)"
chmod 755 "$temporary"
install_root="$temporary/install"
data_root="$temporary/data"
config="$temporary/native.env"
frontend_port=25173
api_port=28000
node_port=28001
mongo_port=27071
deadline=0

check() {
  if ! "$@"; then
    echo "原生部署验证失败：$*" >&2
    return 1
  fi
}

env_value() {
  local key="$1"
  awk -F= -v key="$key" '$1 == key { sub("^[^=]*=", ""); print; exit }' "$config"
}

wait_http() {
  local url="$1" allow_auth="${2:-false}"
  while (( SECONDS < deadline )); do
    local status
    status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 3 "$url" || true)"
    if [[ "$status" =~ ^2 ]] || { [[ "$allow_auth" == true ]] && [[ "$status" =~ ^(401|403)$ ]]; }; then
      return 0
    fi
    sleep 2
  done
  echo "健康检查超时：$url" >&2
  return 1
}

wait_services() {
  local service
  for service in camera-logs-mongo camera-logs-api camera-logs-worker camera-logs-frontend; do
    while (( SECONDS < deadline )); do
      if systemctl is-enabled --quiet "$service" && systemctl is-active --quiet "$service"; then
        break
      fi
      sleep 2
    done
    check systemctl is-enabled --quiet "$service"
    check systemctl is-active --quiet "$service"
  done
}

python_check() {
  local code="$1"
  local python_bin="$install_root/venvs/backend/bin/python"
  [[ -x "$python_bin" ]] || { echo "后端虚拟环境不可用" >&2; return 1; }
  MONGO_URI="$(env_value MONGO_URI)" NODE_ID="$(env_value NODE_ID)" NODE_PORT="$node_port" \
    "$python_bin" -c "$code"
}

mongo_and_worker_ready() {
  python_check 'import asyncio, os, urllib.request
from datetime import datetime, timedelta, timezone
from pymongo import AsyncMongoClient
async def verify():
    client = AsyncMongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=3000)
    try:
        status = await client.admin.command("replSetGetStatus")
        assert status["set"] == "rs0" and sum(item["state"] == 1 for item in status["members"]) == 1
        node = await client.camera_logs.nodes.find_one({"id": os.environ["NODE_ID"]})
        limit = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=75)
        assert node and node["heartbeat"] >= limit
        urllib.request.urlopen("http://127.0.0.1:" + os.environ["NODE_PORT"] + "/health", timeout=3)
    finally:
        await client.close()
asyncio.run(verify())'
}

start_health_deadline() {
  # 安装和构建不消耗健康检查预算，服务启动与重启各有完整等待时间。
  deadline=$((SECONDS + timeout))
}

configure() {
  local python312
  python312="$(command -v python3.12)"
  python3 - "$config" "$install_root" "$data_root" "$frontend_port" "$api_port" "$node_port" "$mongo_port" "$python312" <<'PY'
import re
import sys
from pathlib import Path

path, install, data, frontend, api, node, mongo, python_bin = map(Path if False else str, sys.argv[1:])
values = {}
lines = Path(path).read_text(encoding="utf-8").splitlines()
for line in lines:
    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        values[key] = value
required = {"INSTALL_ROOT", "DATA_ROOT", "LOG_ROOT", "API_LOG_ROOT", "MONGO_DATA_ROOT", "FRONTEND_PORT",
            "API_PORT", "NODE_PORT", "MONGO_PORT", "MONGO_URI", "BACKEND_UPSTREAM", "NODE_URL", "PYTHON_BIN"}
missing = required - values.keys()
if missing:
    raise SystemExit("原生配置缺少验证所需键：" + ",".join(sorted(missing)))
updates = {"INSTALL_ROOT": install, "DATA_ROOT": data, "LOG_ROOT": data + "/collector",
           "API_LOG_ROOT": data + "/api", "MONGO_DATA_ROOT": data + "/mongo",
           "FRONTEND_PORT": frontend, "API_PORT": api, "NODE_PORT": node, "MONGO_PORT": mongo,
           "BACKEND_UPSTREAM": "http://127.0.0.1:" + api, "NODE_URL": "http://127.0.0.1:" + node,
           "PYTHON_BIN": python_bin}
uri = values["MONGO_URI"]
updated_uri, count = re.subn(r"(@[^/]+:)[0-9]{1,5}(?=/)", lambda match: match.group(1) + mongo, uri, count=1)
if count != 1:
    raise SystemExit("无法将 MONGO_URI 限定为临时 Mongo 端口")
updates["MONGO_URI"] = updated_uri
rendered = []
for line in lines:
    if line and not line.startswith("#") and "=" in line:
        key = line.split("=", 1)[0]
        if key in updates:
            line = key + "=" + updates[key]
    rendered.append(line)
Path(path).write_text("\n".join(rendered) + "\n", encoding="utf-8")
PY
  chmod 600 "$config"
}

diagnose() {
  local service
  for service in camera-logs-mongo camera-logs-api camera-logs-worker camera-logs-frontend; do
    systemctl --no-pager --full status "$service" 2>&1 | tail -80 || true
    journalctl --no-pager -u "$service" -n 100 2>&1 || true
  done
}

cleanup() {
  local service
  for service in camera-logs-frontend camera-logs-worker camera-logs-api camera-logs-mongo; do
    systemctl disable --now "$service" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$service.service"
  done
  systemctl daemon-reload
  case "$temporary" in /opt/camera-native-smoke-*) rm -rf -- "$temporary" ;; *) echo "拒绝清理非临时路径" >&2; return 1 ;; esac
  [[ ! -e "$temporary" ]]
}

trap 'result=$?; if (( result != 0 )); then diagnose; cleanup || true; fi; exit "$result"' EXIT

check test -x "$root/deploy-native.sh"
check bash "$root/deploy-native.sh" all --config "$config" --init
configure
first_digest="$(sha256sum "$config" | awk '{print $1}')"
check bash "$root/deploy-native.sh" all --config "$config"
start_health_deadline
wait_services
wait_http "http://127.0.0.1:$api_port/health"
wait_http "http://127.0.0.1:$frontend_port/api/v1/auth/me" true
while (( SECONDS < deadline )); do
  mongo_and_worker_ready && break || true
  sleep 2
done
mongo_and_worker_ready

check bash "$root/deploy-native.sh" all --config "$config"
[[ "$first_digest" == "$(sha256sum "$config" | awk '{print $1}')" ]] || {
  echo "重复部署改写了原生配置或密钥" >&2
  exit 1
}

for service in camera-logs-mongo camera-logs-api camera-logs-worker camera-logs-frontend; do
  check systemctl restart "$service"
done
start_health_deadline
wait_services
wait_http "http://127.0.0.1:$api_port/health"
wait_http "http://127.0.0.1:$frontend_port/api/v1/auth/me" true
while (( SECONDS < deadline )); do
  mongo_and_worker_ready && break || true
  sleep 2
done
mongo_and_worker_ready
cleanup
trap - EXIT
printf '%s\n' '{"passed":true,"nativeDeployment":true,"singleMemberAuthenticatedReplicaSet":true,"repeatPreservesConfiguration":true,"restartHealth":true,"temporaryInstallationRemoved":true}'
