#!/usr/bin/env bash
# 从 Docker 官方 APT 源安装 Engine 与 Compose 插件；仅 Ubuntu/Debian 调用本脚本。
set -euo pipefail

python_only=false
if [[ "${1:-}" == "--python-only" ]]; then
  python_only=true
fi

if [[ "$(uname -s)" != "Linux" ]] || [[ ! -r /etc/os-release ]]; then
  echo "仅支持在 Ubuntu 或 Debian Linux 上自动安装 Docker" >&2
  exit 1
fi

# shellcheck disable=SC1091
. /etc/os-release
if [[ "${ID:-}" != "ubuntu" && "${ID:-}" != "debian" ]]; then
  echo "当前发行版不是 Ubuntu/Debian，请自行安装 Docker Engine 与 Compose 插件" >&2
  exit 1
fi

if [[ "$EUID" -eq 0 ]]; then
  elevate=()
elif command -v sudo >/dev/null 2>&1; then
  elevate=(sudo)
else
  echo "自动安装需要 root 权限；请以 root 运行或先安装 sudo" >&2
  exit 1
fi
"${elevate[@]}" apt-get update
"${elevate[@]}" apt-get install -y python3 ca-certificates curl
if [[ "$python_only" == true ]]; then
  exit 0
fi
"${elevate[@]}" install -m 0755 -d /etc/apt/keyrings
"${elevate[@]}" curl -fsSL "https://download.docker.com/linux/$ID/gpg" -o /etc/apt/keyrings/docker.asc
"${elevate[@]}" chmod a+r /etc/apt/keyrings/docker.asc
codename="${VERSION_CODENAME:-}"
if [[ -z "$codename" ]]; then
  codename="$(. /etc/os-release && echo "$VERSION_CODENAME")"
fi
arch="$(dpkg --print-architecture)"
echo "deb [arch=$arch signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/$ID $codename stable" \
  | "${elevate[@]}" tee /etc/apt/sources.list.d/docker.list > /dev/null
"${elevate[@]}" apt-get update
"${elevate[@]}" apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
"${elevate[@]}" systemctl enable --now docker
if [[ "$EUID" -ne 0 ]]; then
  "${elevate[@]}" usermod -aG docker "$USER"
fi
