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

# systemd 托管时由 systemd 重启；否则回退到 start.sh 后台模式。
if command -v systemctl >/dev/null 2>&1 \
   && systemctl is-enabled --quiet audio-book.service 2>/dev/null; then
  NOVEL_TTS_HOME="${NOVEL_TTS_HOME:-$HOME/.novel-tts}"
  VENV_PY="$NOVEL_TTS_HOME/.venv/bin/python"
  systemctl stop audio-book.service
  if ! "$VENV_PY" -m pip install --quiet --disable-pip-version-check -r backend/requirements.txt; then
    # 依赖安装失败时回滚到旧环境，避免部署把服务留在停机状态。
    systemctl start audio-book.service || true
    echo "后端依赖安装失败，已尝试恢复旧版本服务。" >&2
    exit 1
  fi
  systemctl start audio-book.service
else
  bash ./start.sh --restart
fi
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
HEALTH_URL="http://${HEALTH_HOST}:${PORT:-28000}/api/health"
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 3 "$HEALTH_URL"; then
    echo
    exit 0
  fi
  sleep 1
done
echo "健康检查失败: $HEALTH_URL" >&2
exit 1
