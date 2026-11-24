#!/usr/bin/env bash
# AI 前沿日报：每日自动生成（由 systemd timer 调用）
#
# 鉴权：用后端 .env 里的 DSH_SERVICE_TOKEN（请求头 X-DSH-Token），
# 不需要管理员密码/JWT。脚本只**生成**，不发布；发布前必须人工终审。
#
# 用法：./deploy/daily-generate.sh [date]
#   环境变量：
#     AUDIO_BOOK_URL       默认 http://127.0.0.1:28000
#     NOVEL_TTS_HOME       默认 /root/.novel-tts（从 $NOVEL_TTS_HOME/.env 读 token）
set -euo pipefail

BASE_URL="${AUDIO_BOOK_URL:-http://127.0.0.1:28000}"
NTH="${NOVEL_TTS_HOME:-/root/.novel-tts}"
ENV_FILE="$NTH/.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "[daily] 找不到 $ENV_FILE" >&2
  exit 1
fi
TOKEN="$(grep -E '^DSH_SERVICE_TOKEN=' "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"' | tr -d "'")"
if [ -z "$TOKEN" ]; then
  echo "[daily] $ENV_FILE 未配置 DSH_SERVICE_TOKEN" >&2
  exit 1
fi

DATE="${1:-}"
BODY='{}'
if [ -n "$DATE" ]; then
  BODY="{\"date\":\"$DATE\"}"
fi

echo "[daily] 触发生成 date=${DATE:-today} ..."
curl -sS -m 30 -X POST "$BASE_URL/api/daily/run" \
  -H "X-DSH-Token: $TOKEN" \
  -H 'Content-Type: application/json' \
  -d "$BODY"

# 轮询到结束（最多 30 分钟），失败退出码非 0，交给 systemd/告警
for _ in $(seq 1 180); do
  sleep 10
  STATUS="$(curl -sS -m 30 "$BASE_URL/api/daily/run/status" -H "X-DSH-Token: $TOKEN")"
  STATE="$(printf '%s' "$STATUS" | /usr/bin/env python3 -c 'import sys,json; print(json.load(sys.stdin)["run"]["status"])' 2>/dev/null || echo unknown)"
  if [ "$STATE" != "running" ]; then
    echo "[daily] 结束状态: $STATE"
    printf '%s\n' "$STATUS" | /usr/bin/env python3 -c 'import sys,json; r=json.load(sys.stdin)["run"]; print("error:", r.get("error")); print("\n".join(r.get("log_tail", [])[-10:]))' 2>/dev/null || true
    [ "$STATE" = "success" ] || exit 1
    exit 0
  fi
done
echo "[daily] 超时：生成超过 30 分钟仍未结束" >&2
exit 1
