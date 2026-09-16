#!/usr/bin/env bash
set -euo pipefail

PROJ_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJ_DIR"

# ============================================================================
# 用法:
#   ./start.sh              后台运行（默认；PID 写入 $NOVEL_TTS_HOME/data/uvicorn.pid）
#   ./start.sh --foreground 前台运行（Ctrl+C 停止）
#   ./start.sh --stop       停止后台进程
#   ./start.sh --restart    重启后台进程
#   ./start.sh --status     查看运行状态
#   ./start.sh --paths      打印当前生效的所有路径（data/.venv/.env/uvicorn.pid/log）
#   ./start.sh --help       显示帮助
#
# 升级流程（推荐）:
#   cd /path/to/novel-tts
#   ./start.sh --stop
#   git pull
#   ./start.sh
# 代码与数据彻底分开：data/ .venv/ .env 都在 NOVEL_TTS_HOME（默认 ~/.novel-tts），
# 任何时候 rm -rf 项目目录都不会丢数据；git reset --hard 也不会丢数据。
# ============================================================================

# ---------- NOVEL_TTS_HOME 解析（核心：把运行时产物从项目目录里抽出来） ----------
# 优先级：环境变量 > 用户配置 > 默认 ~/.novel-tts
# 用户可以 export NOVEL_TTS_HOME=/some/where 覆盖；不设置就放 ~/.novel-tts。
DEFAULT_HOME="$HOME/.novel-tts"
NOVEL_TTS_HOME="${NOVEL_TTS_HOME:-$DEFAULT_HOME}"

# 关键路径
DATA_DIR="$NOVEL_TTS_HOME/data"
AUDIO_DIR="$DATA_DIR/audio"
LOG_DIR="$DATA_DIR/logs"
LOGS_APP_FILE="$LOG_DIR/app.log"
PID_FILE="$DATA_DIR/uvicorn.pid"
STDOUT_LOG="$LOG_DIR/uvicorn-stdout.log"
VENV_DIR="$NOVEL_TTS_HOME/.venv"
ENV_FILE="$NOVEL_TTS_HOME/.env"

ACTION="daemon"
case "${1:-}" in
  --daemon|-d)          ACTION="daemon" ;;
  --foreground|-f|--fg) ACTION="foreground" ;;
  --stop)               ACTION="stop" ;;
  --restart)            ACTION="restart" ;;
  --status)             ACTION="status" ;;
  --paths)              ACTION="paths" ;;
  --help|-h)            ACTION="help" ;;
  "")                   ACTION="daemon" ;;
  *) echo "未知参数: $1（支持 --foreground / --daemon / --stop / --restart / --status / --paths / --help）"; exit 1 ;;
esac

is_running() {
  [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

# ---------- 帮助 ----------
if [ "$ACTION" = "help" ]; then
  cat <<'EOF'
start.sh — AI 有声小说生成器启动脚本

子命令:
  --foreground / -f    前台运行（Ctrl+C 停止）
  --daemon / -d        后台运行（默认）
  --stop               停止后台进程
  --restart            重启后台进程
  --status             查看运行状态
  --paths              打印所有生效路径
  --help / -h          显示本帮助

环境变量:
  NOVEL_TTS_HOME       运行时产物根目录（默认 ~/.novel-tts）
                       包含: data/ .venv/ .env

升级流程（不会丢数据）:
  cd /path/to/novel-tts
  ./start.sh --stop
  git pull
  ./start.sh

数据迁移（旧部署升级时，一次性）:
  1. export NOVEL_TTS_HOME=$HOME/.novel-tts
  2. mkdir -p "$NOVEL_TTS_HOME"
  3. mv ./data "$NOVEL_TTS_HOME/data"          # 项目内 data → 外部
  4. mv ./.env "$NOVEL_TTS_HOME/.env"          # 项目内 .env → 外部（如有）
  5. 保留 .venv（重装依赖更快，但建议按需 rm -rf backend/.venv 让脚本重建）
  6. 重新跑 ./start.sh
EOF
  exit 0
fi

# ---------- 路径展示 ----------
if [ "$ACTION" = "paths" ]; then
  cat <<EOF
[paths] 当前生效路径
  NOVEL_TTS_HOME : $NOVEL_TTS_HOME
  DATA_DIR       : $DATA_DIR
  AUDIO_DIR      : $AUDIO_DIR
  LOG_DIR        : $LOG_DIR
  PID_FILE       : $PID_FILE
  STDOUT_LOG     : $STDOUT_LOG
  LOGS_APP_FILE  : $LOGS_APP_FILE
  VENV_DIR       : $VENV_DIR
  ENV_FILE       : $ENV_FILE
EOF
  exit 0
fi

# ---------- 首次启动准备：创建目录 ----------
mkdir -p "$DATA_DIR" "$AUDIO_DIR" "$LOG_DIR"

# ---------- 启动前提示数据位置（用户友好；首次启动可一眼看清） ----------
if [ ! -f "$NOVEL_TTS_HOME/.first_run_acknowledged" ]; then
  echo "[paths] 数据/venv/.env 都在 $NOVEL_TTS_HOME（项目目录外，git reset 安全）"
fi

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
    echo "[status] 运行中 (pid=$(cat "$PID_FILE"))，日志: $STDOUT_LOG 与 $LOGS_APP_FILE"
  else
    echo "[status] 未运行"
    exit 1
  fi
  exit 0
fi

# ---------- 启动前检查：已有后台实例则拒绝重复启动（避免端口冲突） ----------
if { [ "$ACTION" = "daemon" ] || [ "$ACTION" = "foreground" ]; } && is_running; then
  echo "已有后台进程在运行 (pid=$(cat "$PID_FILE"))。如需重启: ./start.sh --restart；或先 ./start.sh --stop"
  exit 1
fi

# ---------- .env 加载顺序：优先 NOVEL_TTS_HOME/.env，回退到项目内 .env ----------
# 兼容旧部署：旧版 .env 在项目根，新版挪到 NOVEL_TTS_HOME。
if [ ! -f "$ENV_FILE" ] && [ -f .env ]; then
  echo "[init] 未找到 $ENV_FILE，从项目内 .env 复制（仅一次性兼容）"
  cp .env ./.env.local-backup
  cp .env "$ENV_FILE"
  echo "[init] 已迁移到 $ENV_FILE；项目内 .env 保留为 .env.local-backup（可手动删除）"
fi
if [ ! -f "$ENV_FILE" ] && [ ! -f .env ]; then
  echo "[init] 未找到 .env，从 .env.example 复制，请检查 API Key"
  cp .env.example "$ENV_FILE"
fi
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

# ---------- 环境变量配置：把运行时产物路径 export 到进程 env ----------
# 注意：必须在 source "$ENV_FILE" 之后 export，否则 .env 里的 DATA_DIR=./data 等
# 字段会覆盖掉我们的 NOVEL_TTS_HOME 派生路径。
# Pydantic BaseSettings 优先级：init kwargs > 环境变量 > dotenv > 默认值，
# 所以这里 export 的值后端会自动读到（无需改后端代码）。
export DATA_DIR="$DATA_DIR"
export AUDIO_DIR="$AUDIO_DIR"
export DATABASE_URL="sqlite+aiosqlite:///$DATA_DIR/app.db"
export LOG_FILE="$LOGS_APP_FILE"

BIND_HOST="${BIND_HOST:-127.0.0.1}"
PORT="${PORT:-28000}"
UVICORN_TIMEOUT="${UVICORN_TIMEOUT:-600}"
if [ "$UVICORN_TIMEOUT" = "0" ]; then
  echo "[WARNING] UVICORN_TIMEOUT=0（keep-alive 无穷大）会导致僵尸连接堆积，建议改为 300~3600。" >&2
fi

# ---------- 前端构建 ----------
if [ ! -d frontend/out ] || [ ! -f frontend/out/index.html ]; then
  echo "[frontend] 首次构建 Next.js（static export）…"
  if [ ! -d frontend/node_modules ]; then
    (cd frontend && npm install --no-audit --no-fund --loglevel=error)
  fi
  (cd frontend && npm run build)
fi

# ---------- venv + 依赖（建在 NOVEL_TTS_HOME/.venv；不污染项目目录） ----------
if [ ! -d "$VENV_DIR" ]; then
  echo "[backend] 创建 Python 3.11+ venv 于 $VENV_DIR …"
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
  "$PY" -m venv "$VENV_DIR"
fi

# venv 布局兼容：Linux/macOS = bin/，Windows = Scripts/
if [ -f "$VENV_DIR/bin/python" ]; then
  VENV_PY="$VENV_DIR/bin/python"
elif [ -f "$VENV_DIR/Scripts/python.exe" ]; then
  VENV_PY="$VENV_DIR/Scripts/python.exe"
else
  echo "ERROR: venv 损坏（找不到 python 可执行文件），删除 $VENV_DIR 后重试。"
  exit 1
fi

"$VENV_PY" -m pip install --quiet --disable-pip-version-check -r backend/requirements.txt

# ---------- 启动 ----------
UVICORN_ARGS=(
  backend.app.main:app
  --host "$BIND_HOST"
  --port "$PORT"
  --workers 1
  --timeout-keep-alive "$UVICORN_TIMEOUT"
  --loop asyncio
)

if [ "$ACTION" = "daemon" ]; then
  : > "$STDOUT_LOG"
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
    echo "  PID   $DAEMON_PID（$PID_FILE）"
    echo "  访问  http://${BIND_HOST}:${PORT}/"
    echo "  日志  $STDOUT_LOG / $LOGS_APP_FILE"
    echo "  数据  $NOVEL_TTS_HOME（项目目录外，git pull / rm -rf 安全）"
    echo "  路径  ./start.sh --paths"
    echo "  停止  ./start.sh --stop    重启: ./start.sh --restart"
    echo "=========================================="
    touch "$NOVEL_TTS_HOME/.first_run_acknowledged"
  else
    echo "[daemon] 进程在运行但端口 ${PORT} 暂未响应（首次构建/冷启动可能较慢），稍后用 ./start.sh --status 检查" >&2
  fi
  exit 0
fi

# ---------- 前台运行 ----------
echo ""
echo "=========================================="
echo "  AI 有声小说生成器"
echo "  访问 http://${BIND_HOST}:${PORT}/"
echo "  数据  $NOVEL_TTS_HOME"
echo "  Ctrl+C 停止   （默认后台运行: ./start.sh）"
echo "=========================================="
exec "$VENV_PY" -m uvicorn "${UVICORN_ARGS[@]}"