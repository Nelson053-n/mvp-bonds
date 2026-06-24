#!/usr/bin/env bash
# Авто-деплой bondai: вызывается systemd timer'ом каждые N минут.
#
# Логика:
#   1. git fetch origin v3
#   2. Если origin/v3 впереди HEAD — запустить ops/deploy.sh
#   3. Отправить TG-уведомление при успехе/провале деплоя
#   4. Логировать результат в /var/log/bondai-autodeploy.log
#
# Переменные берутся из /opt/mvp-bonds/.env (те же, что у сервиса).
# Требует git, curl; .venv уже есть на проде.

set -euo pipefail

REPO_DIR="/opt/mvp-bonds"
LOG_FILE="/var/log/bondai-autodeploy.log"
DEPLOY_SCRIPT="$REPO_DIR/ops/deploy.sh"
BRANCH="v3"

cd "$REPO_DIR"

# Читаем .env для TG
set -a
# shellcheck disable=SC1091
[ -f .env ] && . ./.env
set +a

TG_TOKEN="${MVP_TG_BOT_TOKEN:-}"
# tg_chat_id хранится в app_settings SQLite (туда же, куда пишет UI)
DB_PATH="${MVP_SQLITE_DB_PATH:-$REPO_DIR/data/portfolio.db}"
TG_CHAT="$(sqlite3 "$DB_PATH" "SELECT value FROM app_settings WHERE key='tg_chat_id' LIMIT 1" 2>/dev/null || true)"

_log() {
  local ts
  ts="$(date '+%Y-%m-%d %H:%M:%S')"
  echo "[$ts] $*" | tee -a "$LOG_FILE"
}

_tg() {
  local msg="$1"
  [ -z "$TG_TOKEN" ] || [ -z "$TG_CHAT" ] && return 0
  curl -sS --max-time 10 \
    -d "chat_id=${TG_CHAT}&text=${msg}&parse_mode=HTML" \
    "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
    > /dev/null 2>&1 || true
}

# --- 1. fetch ---
git fetch origin "$BRANCH" --quiet

# --- 2. сравниваем HEAD с origin/v3 ---
LOCAL_HEAD="$(git rev-parse HEAD)"
REMOTE_HEAD="$(git rev-parse "origin/$BRANCH")"

if [ "$LOCAL_HEAD" = "$REMOTE_HEAD" ]; then
  # Нечего деплоить — тихо выходим (таймер запускается часто, молчим в норме)
  exit 0
fi

BEHIND="$(git rev-list HEAD..origin/$BRANCH --count)"
_log "origin/$BRANCH впереди на $BEHIND коммит(ов): $LOCAL_HEAD → $REMOTE_HEAD — деплоим…"

# --- 3. деплоим через атомарный скрипт ---
DEPLOY_LOG="$(bash "$DEPLOY_SCRIPT" 2>&1)" && DEPLOY_OK=1 || DEPLOY_OK=0

NEW_HEAD="$(git rev-parse HEAD)"
SHORT_NEW="${NEW_HEAD:0:8}"
SHORT_OLD="${LOCAL_HEAD:0:8}"

if [ "$DEPLOY_OK" = "1" ]; then
  _log "✓ авто-деплой ок: $SHORT_OLD → $SHORT_NEW (+$BEHIND коммит(ов))"
  _tg "✅ <b>bondai авто-деплой</b>%0A%2B$BEHIND коммит(ов): <code>$SHORT_OLD → $SHORT_NEW</code>"
else
  _log "✗ авто-деплой ПРОВАЛИЛСЯ (deploy.sh вернул ненулевой код)"
  _log "$DEPLOY_LOG"
  _tg "❌ <b>bondai авто-деплой ПРОВАЛИЛСЯ</b>%0A<code>$SHORT_OLD → $SHORT_NEW</code>%0AПроверь journalctl -u bondai"
  exit 1
fi
