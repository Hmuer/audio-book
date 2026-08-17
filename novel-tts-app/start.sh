#!/usr/bin/env bash
set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJ_DIR"

# 0. 根 .env
if [ ! -f .env ]; then
  echo "[init] 未找到 .env，从 .env.example 复制，请检查 API Key"
  cp .env.example .env
fi
# 导出 .env 变量
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

BIND_HOST="${BIND_HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
# ⚠️  UVICORN_TIMEOUT 语义：ASGI/uvicorn 侧 **TCP keep-alive 空闲超时**（秒）。
#     它 ≠ 「一个 HTTP 请求允许跑多久」。想让一个接口撑 30 分钟，应走 202+后台任务+轮询
#     （prepare / builds 已经是这种模式），不要靠把这里改成 0（无穷大）解决。
#     设成 0 的副作用：半关闭/断网/NAT 超时后产生的僵尸 TCP 连接永远不回收，
#     最终吃满文件句柄 / asyncio 事件循环负载。
UVICORN_TIMEOUT="${UVICORN_TIMEOUT:-600}"
if [ "$UVICORN_TIMEOUT" = "0" ]; then
  echo "[WARNING] UVICORN_TIMEOUT=0（keep-alive 无穷大）会导致僵尸连接堆积，建议改为 300~3600。" >&2
fi

# 1. 目录
mkdir -p data/audio

# 2. 前端构建（缓存）
if [ ! -d frontend/out ] || [ ! -f frontend/out/index.html ]; then
  echo "[frontend] 首次构建 Next.js（static export）…"
  if [ ! -d frontend/node_modules ]; then
    (cd frontend && npm install --no-audit --no-fund --loglevel=error)
  fi
  (cd frontend && npm run build)
fi

# 3. 后端 venv + 依赖
if [ ! -d backend/.venv ]; then
  echo "[backend] 创建 Python 3.11 venv …"
  PY=""
  for cand in python3.11 python3.12 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      ver=$("$cand" -c 'import sys; print("%d.%d" % sys.version_info[:2])')
      major=${ver%%.*}
      if [ "$major" = "3" ]; then
        minor=${ver#*.}
        if [ "$minor" -ge 11 ]; then
          PY="$cand"
          break
        fi
      fi
    fi
  done
  if [ -z "$PY" ]; then
    echo "ERROR: 找不到 Python 3.11+。请先安装。"
    exit 1
  fi
  "$PY" -m venv backend/.venv
fi

# shellcheck disable=SC1091
source backend/.venv/bin/activate
pip install --quiet --disable-pip-version-check -r backend/requirements.txt

# 4. 启动单进程 uvicorn
echo ""
echo "=========================================="
echo "  AI 有声小说生成器"
echo "  访问 http://${BIND_HOST}:${PORT}/"
echo "  Docs  http://${BIND_HOST}:${PORT}/docs"
echo "  Ctrl+C 停止"
echo "=========================================="
exec uvicorn backend.app.main:app \
  --host "$BIND_HOST" \
  --port "$PORT" \
  --workers 1 \
  --timeout-keep-alive "$UVICORN_TIMEOUT" \
  --loop asyncio
