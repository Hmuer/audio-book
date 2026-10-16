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
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:28000/api/health
echo
