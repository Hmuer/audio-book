#!/usr/bin/env bash
set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJ_DIR"

# ============================================================================
# 用法:
#   ./start.sh             前台运行（默认，Ctrl+C 停止）
#   ./start.sh --daemon    后台运行（nohup，PID 写入 data/uvicorn.pid）
#   ./start.sh --stop      停止后台进程
#   ./start.sh --restart   重启后台进程
#   ./start.sh --status    查看运行状态
# 也可用环境变量: DAEMON=1 ./start.sh 等价于 --daemon
# ============================================================================

PID_FILE="data/uvicorn.pid"
STDOUT_LOG="data/logs/uvicorn-stdout.log"   # 启动期/崩溃期 stdout；运行日志在 data/logs/app.log

ACTION="foreground"
case "${1:-}" in
  --daemon|-d)  ACTION="daemon" ;;
  --stop)       ACTION="stop" ;;
  --restart)    ACTION="restart" ;;
  --status)     ACTION="status" ;;
  "")           ACTION="${DAEMON:+daemon}"; ACTION="${ACTION:-foreground}" ;;
  *) echo "未知参数: $1（支持 --daemon / --stop / --restart / --status）"; exit 1 ;;
esac

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

# ---------- 停止 ----------
if [ "$ACTION" = "stop" ] || [ "$ACTION" = "restart" ]; then
  if is_running; then
    PID=$(cat "$PID_FILE")
    echo "[stop] 停止 uvicorn (pid=$PID) …"
    kill "$PID" 2>/dev/null || true
    for _ in $(seq 1 15); do
      kill -0 "$PID" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$PID" 2>/dev/null; then
      echo "[stop] 3s 未退出，强制结束"
      kill -9 "$PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
    echo "[stop] 已停止"
  else
    rm -f "$PID_FILE"
    echo "[stop] 没有在运行的后台进程（前台模式请到对应终端 Ctrl+C）"
  fi
  [ "$ACTION" = "stop" ] && exit 0
fi

# ---------- 状态 ----------
if [ "$ACTION" = "status" ]; then
  if is_running; then
    echo "[status] 运行中 (pid=$(cat "$PID_FILE"))，日志: $STDOUT_LOG 与 data/logs/app.log"
  else
    echo "[status] 未运行"
    exit 1
  fi
  exit 0
fi

# ---------- 启动前检查：已有后台实例则拒绝重复启动 ----------
if [ "$ACTION" = "daemon" ] && is_running; then
  echo "[daemon] 已有后台进程在运行 (pid=$(cat "$PID_FILE"))。如需重启: ./start.sh --restart"
  exit 1
fi

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
PORT="${PORT:-28000}"
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
mkdir -p data/audio data/logs

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

# venv 布局兼容：Linux/macOS = bin/，Windows = Scripts/
if [ -f backend/.venv/bin/python ]; then
  VENV_PY="backend/.venv/bin/python"
elif [ -f backend/.venv/Scripts/python.exe ]; then
  VENV_PY="backend/.venv/Scripts/python.exe"
else
  echo "ERROR: venv 损坏（找不到 python 可执行文件），删除 backend/.venv 后重试。"
  exit 1
fi

"$VENV_PY" -m pip install --quiet --disable-pip-version-check -r backend/requirements.txt

UVICORN_ARGS=(
  backend.app.main:app
  --host "$BIND_HOST"
  --port "$PORT"
  --workers 1
  --timeout-keep-alive "$UVICORN_TIMEOUT"
  --loop asyncio
)

# ---------- 后台运行 ----------
if [ "$ACTION" = "daemon" ]; then
  : > "$STDOUT_LOG"
  # nohup + & 脱离终端；venv 的 python -m uvicorn 避免 PATH/激活脚本平台差异
  nohup "$VENV_PY" -m uvicorn "${UVICORN_ARGS[@]}" >> "$STDOUT_LOG" 2>&1 &
  DAEMON_PID=$!
  echo "$DAEMON_PID" > "$PID_FILE"
  disown "$DAEMON_PID" 2>/dev/null || true

  # 启动探活：进程活着 + 端口能响应才报成功
  probe_ok=0
  for _ in $(seq 1 30); do
    if ! kill -0 "$DAEMON_PID" 2>/dev/null; then
      echo "[daemon] 启动失败，最后 30 行日志：" >&2
      tail -30 "$STDOUT_LOG" >&2 || true
      rm -f "$PID_FILE"
      exit 1
    fi
    if command -v curl >/dev/null 2>&1; then
      if curl -sf -o /dev/null --max-time 2 "http://${BIND_HOST}:${PORT}/api/health" \
         || curl -sf -o /dev/null --max-time 2 "http://${BIND_HOST}:${PORT}/"; then
        probe_ok=1
        break
      fi
    else
      # 无 curl：给应用 5 秒初始化后仅确认进程存活
      sleep 5
      probe_ok=1
      break
    fi
    sleep 1
  done

  echo ""
  if [ "$probe_ok" = "1" ]; then
    echo "=========================================="
    echo "  AI 有声小说生成器（后台运行）"
    echo "  PID   $DAEMON_PID（data/uvicorn.pid）"
    echo "  访问  http://${BIND_HOST}:${PORT}/"
    echo "  日志  $STDOUT_LOG / data/logs/app.log"
    echo "  停止  ./start.sh --stop    重启: ./start.sh --restart"
    echo "=========================================="
  else
    echo "[daemon] 进程在运行但端口 ${PORT} 暂未响应（首次构建/冷启动可能较慢），稍后用 ./start.sh --status 检查" >&2
  fi
  exit 0
fi

# ---------- 前台运行（默认） ----------
echo ""
echo "=========================================="
echo "  AI 有声小说生成器"
echo "  访问 http://${BIND_HOST}:${PORT}/"
echo "  Docs  http://${BIND_HOST}:${PORT}/docs"
echo "  Ctrl+C 停止   （后台运行: ./start.sh --daemon）"
echo "=========================================="
exec "$VENV_PY" -m uvicorn "${UVICORN_ARGS[@]}"
