#!/bin/bash
# 在 Ubuntu 上安装依赖、构建页面、启动唯一的 API 容器，并给域名申请 HTTPS。
# 用法：sudo bash deploy/setup.sh 你的域名
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "请用 sudo 运行：sudo bash deploy/setup.sh 你的域名"
  exit 1
fi

DOMAIN="${1:-}"
if [[ ! "$DOMAIN" =~ ^[A-Za-z0-9.-]+$ ]]; then
  echo "用法：sudo bash deploy/setup.sh 你的域名"
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -f .env ]]; then
  echo "项目目录缺少 .env。请从自己的电脑把 .env 和代码一起拷过来。"
  exit 1
fi

missing=()
for key in CLOUDBASE_ENV_ID CLOUDBASE_API_KEY DEEPSEEK_API_KEY; do
  if ! grep -Eq "^${key}=.+" .env; then
    missing+=("$key")
  fi
done
if ((${#missing[@]})); then
  echo ".env 里这些项还是空的：${missing[*]}"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y ca-certificates curl nginx certbot python3-certbot-nginx python3 xz-utils

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  apt-get install -y docker.io docker-compose-v2
fi
systemctl enable --now docker

install_node() {
  local arch node_arch version url
  arch="$(uname -m)"
  case "$arch" in
    x86_64) node_arch="linux-x64" ;;
    aarch64) node_arch="linux-arm64" ;;
    *)
      echo "这台机器的 CPU 架构是 ${arch}，脚本只准备了 x86_64 和 arm64。"
      exit 1
      ;;
  esac
  version="v22.21.0"
  url="https://cdn.npmmirror.com/binaries/node/${version}/node-${version}-${node_arch}.tar.xz"
  if ! curl -fL --retry 3 -o /tmp/node.tar.xz "$url"; then
    url="https://nodejs.org/dist/${version}/node-${version}-${node_arch}.tar.xz"
    curl -fL --retry 3 -o /tmp/node.tar.xz "$url"
  fi
  tar -C /usr/local --strip-components=1 -xJf /tmp/node.tar.xz
  rm -f /tmp/node.tar.xz
}

need_node=0
if ! command -v node >/dev/null 2>&1; then
  need_node=1
else
  node_major="$(node -p "Number(process.versions.node.split('.')[0])")"
  node_minor="$(node -p "Number(process.versions.node.split('.')[1])")"
  if [[ "$node_major" -lt 20 ]] || [[ "$node_major" -eq 20 && "$node_minor" -lt 19 ]] || [[ "$node_major" -eq 21 ]] || [[ "$node_major" -eq 22 && "$node_minor" -lt 12 ]]; then
    need_node=1
  fi
fi
if [[ "$need_node" -eq 1 ]]; then
  install_node
fi

echo "正在构建页面，这一步需要几分钟。"
npm ci
npm run build
mkdir -p /var/www/pingjian
find /var/www/pingjian -mindepth 1 -delete
cp -a dist/. /var/www/pingjian/

echo "正在构建并启动 API 容器。第一次会下载依赖，可能要十几分钟。"
docker compose up -d --build

ready=0
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18; do
  if curl -fsS http://127.0.0.1:8787/api/health -o /tmp/pingjian-health.json; then
    ready=1
    break
  fi
  sleep 5
done
if [[ "$ready" -ne 1 ]]; then
  echo "容器已启动，但 /api/health 还没有响应。可执行 docker compose logs 查看原因。"
  exit 1
fi
python3 - <<'PY'
import json
with open("/tmp/pingjian-health.json", encoding="utf-8") as handle:
    body = json.load(handle)
missing = body.get("missing") or []
startup = body.get("startupError")
print("健康检查已通过。")
if missing:
    print("仍缺少这些配置：" + ", ".join(missing))
if startup:
    print("启动时有错误：" + str(startup))
PY
rm -f /tmp/pingjian-health.json

sed "s/__DOMAIN__/${DOMAIN}/g" deploy/nginx.conf.template > /etc/nginx/sites-available/pingjian
ln -sfn /etc/nginx/sites-available/pingjian /etc/nginx/sites-enabled/pingjian
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable nginx
systemctl reload nginx

echo "正在为 ${DOMAIN} 申请 HTTPS 证书。"
certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos --register-unsafely-without-email --redirect

echo "完成。在浏览器打开 https://${DOMAIN} ，并再看一次 https://${DOMAIN}/api/health"
