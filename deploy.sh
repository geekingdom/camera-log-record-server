#!/usr/bin/env bash
# Linux 一键部署入口：保留已有环境和卷，构建并验证完整 Compose 服务。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project="${COMPOSE_PROJECT_NAME:-camera-log-record-server}"
compose_file="$root/deploy/docker-compose.yml"
env_file="$root/.env"

if [[ "${1:-}" == "--help" ]]; then
  cat <<'EOF'
用法：./deploy.sh

在 Linux 上准备 Docker Compose、首次创建权限 0600 的 .env，并启动 camera-log-record-server。
可用 COMPOSE_PROJECT_NAME 覆盖默认稳定项目名；已有 .env 和同项目卷会被保留。
EOF
  exit 0
fi

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "一键部署仅支持 Linux" >&2
  exit 1
fi

docker_installed=false
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  if [[ -r /etc/os-release ]] && grep -Eq '^ID=(ubuntu|debian)$' /etc/os-release; then
    "$root/scripts/deploy_docker.sh"
    docker_installed=true
  else
    echo "未找到 Docker Compose；请先在当前 Linux 发行版安装 Docker Engine 与 Compose 插件" >&2
    exit 1
  fi
fi
docker compose version >/dev/null
if ! command -v python3 >/dev/null 2>&1; then
  if [[ -r /etc/os-release ]] && grep -Eq '^ID=(ubuntu|debian)$' /etc/os-release; then
    "$root/scripts/deploy_docker.sh" --python-only
  else
    echo "未找到 python3；请先安装 Python 3 后重试" >&2
    exit 1
  fi
fi
if [[ "$docker_installed" == true && "$EUID" -ne 0 ]]; then
  if ! command -v sudo >/dev/null 2>&1; then
    echo "Docker 已安装但当前会话尚无 docker 组权限，需以 root 重新运行部署" >&2
    exit 1
  fi
  # 仅保留会影响 Compose 展开和健康检查的非敏感覆盖，避免新 Docker 组会话丢失端口等部署参数。
  exec sudo --preserve-env=COMPOSE_PROJECT_NAME,DEPLOY_HEALTH_TIMEOUT,FRONTEND_PORT,COMPOSE_MONGO_URI,DATABASE_NAME,COLLECTOR_NODE_ID,COLLECTOR_NODE_URL,KNOWN_HOSTS_FILE "$root/deploy.sh"
fi
if ! docker info >/dev/null 2>&1; then
  echo "当前用户无法访问 Docker daemon；请确认 Docker 服务运行且账号已加入 docker 组后重试" >&2
  exit 1
fi

if [[ -L "$env_file" ]]; then
  echo "部署环境文件不能是符号链接：$env_file" >&2
  exit 1
fi
if [[ ! -f "$env_file" ]]; then
  existing_volumes="$(docker volume ls -q --filter "label=com.docker.compose.project=$project")"
  if [[ -n "$existing_volumes" ]]; then
    echo "检测到项目 $project 的既有数据卷但缺少 .env；拒绝生成新密钥以保护现有数据" >&2
    exit 1
  fi
  python3 "$root/scripts/deploy_env.py" --output "$env_file"
  echo "初始管理员凭据已保存在 ${env_file}（权限 0600），脚本不会输出密码或令牌"
fi
chmod 600 "$env_file"

docker compose --env-file "$env_file" --project-name "$project" --file "$compose_file" up --build --detach --remove-orphans
python3 "$root/scripts/deploy_health.py" --project "$project" --compose-file "$compose_file" --env-file "$env_file"
