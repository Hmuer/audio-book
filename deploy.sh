#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

if [ -n "$(git status --porcelain)" ]; then
  echo "工作区有未提交的修改，部署已停止。" >&2
  exit 1
fi

git pull --ff-only origin main

# start.sh 只在 out 缺失时构建；更新已有前端代码后必须主动重建。
(cd frontend && npm ci --no-audit --no-fund --loglevel=error && npm run build)

bash ./start.sh --restart
APP_ENV_FILE="${NOVEL_TTS_HOME:-$HOME/.novel-tts}/.env"
if [ -f "$APP_ENV_FILE" ]; then
  # 与 start.sh 使用同一份配置，健康检查应跟随自定义端口。
  set -a
  # shellcheck disable=SC1090
  source "$APP_ENV_FILE"
  set +a
fi
HEALTH_HOST="${BIND_HOST:-127.0.0.1}"
if [ "$HEALTH_HOST" = "0.0.0.0" ]; then HEALTH_HOST="127.0.0.1"; fi
if [ "$HEALTH_HOST" = "::" ]; then HEALTH_HOST="[::1]"; fi
curl --fail --silent --show-error --max-time 10 "http://${HEALTH_HOST}:${PORT:-28000}/api/health"
echo
