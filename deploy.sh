#!/usr/bin/env bash
# Linux 统一部署入口。默认一次部署前端、API、采集节点、三成员数据库。
# 可修改配置放在 .env，详见 deploy/config/*.env.example；不要在脚本中填写密码。
# HOST_LOG_ROOT 控制宿主机采集日志目录；API_DATA_ROOT 控制后端运行日志与临时文件。
# 保留已有环境和数据；重复执行不会重置管理员密码，也不会删除日志或数据库卷。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
component=all
if [[ "${1:-}" == "--component" ]]; then
  component="${2:-}"
  shift 2
fi
case "$component" in all|frontend|backend|worker|database) ;; *) echo "未知部署组件" >&2; exit 2 ;; esac
project="${COMPOSE_PROJECT_NAME:-camera-log-record-server}"
compose_file="$root/deploy/docker-compose.yml"
env_file="$root/.env"
if [[ "$component" != all ]]; then
  # 即使用户给四个入口设置同一基名，也以组件后缀隔离，避免同名初始化服务互相覆盖。
  project="${COMPOSE_PROJECT_NAME:-camera-log-record-server}-$component"
  compose_file="$root/deploy/$component.yml"
  env_file="$root/.env.$component"
fi
multi_host=false
initialize=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file)
      if [[ -z "${2:-}" ]]; then echo "--env-file 缺少配置文件路径" >&2; exit 2; fi
      env_file="$2"; shift 2 ;;
    --multi-host) multi_host=true; shift ;;
    --init) initialize=true; shift ;;
    --help|-h)
  cat <<'EOF'
用法：./deploy.sh [--env-file /绝对路径/部署.env] [--init] [--multi-host]
独立入口：./deploy-frontend.sh、./deploy-backend.sh、./deploy-worker.sh、./deploy-database.sh

在 Linux 上准备 Docker Compose、首次创建权限 0600 的 .env，并启动 camera-log-record-server。
可用 COMPOSE_PROJECT_NAME 覆盖默认稳定项目名；已有 .env 和同项目卷会被保留。
--init 只生成配置，不安装或启动任何服务；编辑配置后再执行部署。
--multi-host 仅用于首次在服务器A创建完整平台跨机配置；生成后DEPLOY_TOPOLOGY=multi-host使重跑自动预检。
可自定义项（含日志目录、端口、远程地址、保留天数）见 deploy/config 与 docs/deployment.md。
EOF
  exit 0
      ;;
    *) echo "不支持的参数，请使用 --help" >&2; exit 2 ;;
  esac
done
if [[ "$initialize" == true ]]; then
  python3 "$root/scripts/deploy_env.py" --output "$env_file" --component "$component"
  if [[ "$multi_host" == true ]]; then
    if [[ "$component" == all ]]; then
      python3 "$root/scripts/deploy_cluster.py" init "$env_file"
    elif [[ "$component" == worker ]]; then
      python3 "$root/scripts/deploy_cluster.py" worker-init "$env_file"
    else
      echo "--multi-host 仅支持 deploy-all.sh 或 deploy-worker.sh" >&2
      exit 2
    fi
  fi
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
  exec sudo --preserve-env=COMPOSE_PROJECT_NAME,DEPLOY_HEALTH_TIMEOUT,FRONTEND_PORT,COMPOSE_MONGO_URI,DATABASE_NAME,COLLECTOR_NODE_ID,COLLECTOR_NODE_URL,KNOWN_HOSTS_FILE,HOST_LOG_ROOT,API_DATA_ROOT,PIP_INDEX_URL,PIP_TRUSTED_HOST "$root/deploy.sh" --component "$component" --env-file "$env_file"
fi
# systemd 主机除了容器 restart 策略，还必须启用 Docker daemon 的开机启动。
# 在已有 Docker 的服务器上也执行；容器内/非 systemd 环境由宿主机管理 Docker。
if [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null 2>&1; then
  if [[ "$EUID" -eq 0 ]]; then
    systemctl enable --now docker
  elif systemctl is-enabled --quiet docker && systemctl is-active --quiet docker; then
    : # Docker 已开机启用且正在运行，docker组用户无需重复提权。
  elif ! command -v sudo >/dev/null 2>&1; then
    echo "需要root权限启用Docker开机启动；请以root运行或预先执行systemctl enable --now docker" >&2
    exit 1
  else
    sudo systemctl enable --now docker || { echo "未能启用Docker开机启动，请由管理员执行systemctl enable --now docker" >&2; exit 1; }
  fi
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
  python3 "$root/scripts/deploy_env.py" --output "$env_file" --component "$component"
  echo "部署配置已保存在 ${env_file}（权限 0600），脚本不会输出密码或令牌"
fi
chmod 600 "$env_file"
# Compose 的 env_file 与变量展开必须指向同一份配置；不 source 配置，避免执行其中内容。
env_file="$(cd "$(dirname "$env_file")" && pwd)/$(basename "$env_file")"
export DEPLOY_ENV_FILE="$env_file"

# 首次 A/B 用 --multi-host 创建标记；以后按 dotenv 解析值自动预检，拒绝未知模式。
if [[ ( "$component" == all || "$component" == worker ) ]] && grep -Eq '^[[:space:]]*(export[[:space:]]+)?DEPLOY_TOPOLOGY[[:space:]]*=' "$env_file"; then
  topology="$(python3 "$root/scripts/deploy_cluster.py" topology "$env_file")"
  case "$topology" in
    "") ;;
    multi-host) multi_host=true ;;
    *) echo "DEPLOY_TOPOLOGY 只能为空或 multi-host" >&2; exit 1 ;;
  esac
fi
if [[ "$multi_host" == true && "$component" == all ]]; then
  # 跨机认证合同只读取指定 env-file，防止调用者 shell 的同名密码覆盖 Compose 展开结果。
  unset DATABASE_NAME MONGO_ROOT_USERNAME MONGO_ROOT_PASSWORD MONGO_REPLICA_KEY MONGO_URI COMPOSE_MONGO_URI
  python3 "$root/scripts/deploy_cluster.py" platform "$env_file"
  "$root/deploy.sh" --component database --env-file "$env_file"
  "$root/deploy.sh" --component backend --env-file "$env_file"
  "$root/deploy.sh" --component worker --env-file "$env_file"
  exec "$root/deploy.sh" --component frontend --env-file "$env_file"
fi
if [[ "$multi_host" == true && "$component" == worker ]]; then
  unset DATABASE_NAME MONGO_ROOT_USERNAME MONGO_ROOT_PASSWORD MONGO_REPLICA_KEY MONGO_URI COMPOSE_MONGO_URI
  python3 "$root/scripts/deploy_cluster.py" worker "$env_file"
fi

# 单机完整平台始终采用认证 MongoDB；跨机路径会在上方递归组件部署后 exec 退出。
if [[ "$component" == all ]]; then
  unset DATABASE_NAME MONGO_ROOT_USERNAME MONGO_ROOT_PASSWORD MONGO_REPLICA_KEY MONGO_URI COMPOSE_MONGO_URI
  python3 "$root/scripts/deploy_cluster.py" single-host "$env_file"
fi

# 配置器只维护项目专属 exports；空地址禁用 NFS，并仅撤销遗留的本项目导出。
configure_nfs() {
  local nfs_export_file="/etc/exports.d/camera-logs-coredump.exports"
  # 未启用时只撤销本项目遗留导出。普通用户在文件存在时也须经 sudo 写系统目录；
  # 文件不存在则配置器无副作用，可直接执行而不额外要求管理员权限。
  if ! grep -Eq '^NFS_SERVER_IP=[^[:space:]#]' "$env_file"; then
    if [[ -e "$nfs_export_file" && "$EUID" -ne 0 ]]; then
      if ! command -v sudo >/dev/null 2>&1; then
        echo "撤销遗留NFS导出需要root权限；请以root运行或安装sudo" >&2
        exit 1
      fi
      sudo python3 "$root/scripts/configure_nfs_export.py" --env-file "$env_file"
    else
      python3 "$root/scripts/configure_nfs_export.py" --env-file "$env_file"
    fi
    return
  fi
  if [[ "$EUID" -eq 0 ]]; then
    python3 "$root/scripts/configure_nfs_export.py" --env-file "$env_file" --install-package
  elif command -v sudo >/dev/null 2>&1; then
    sudo python3 "$root/scripts/configure_nfs_export.py" --env-file "$env_file" --install-package
  else
    echo "已启用NFS但当前无root权限；请以root运行或安装sudo" >&2
    exit 1
  fi
}

if [[ "$component" == all ]]; then
  configure_nfs
  docker compose --env-file "$env_file" --project-name "$project" --file "$compose_file" up --build --detach --remove-orphans
  python3 "$root/scripts/deploy_health.py" --project "$project" --compose-file "$compose_file" --env-file "$env_file"
else
  # 独立部署不使用 --remove-orphans，避免误删同项目已运行的其它服务。
  if [[ "$component" == worker ]]; then configure_nfs; fi
  python3 "$root/scripts/deploy_component.py" --component "$component" --project "$project" --compose-file "$compose_file" --env-file "$env_file"
fi
