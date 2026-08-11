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

# Читаем .env (MVP_SQLITE_DB_PATH и пр.)
set -a
# shellcheck disable=SC1091
[ -f .env ] && . ./.env
set +a

# Токен и chat_id бота — в app_settings SQLite (туда же пишет UI панели);
# sqlite3 CLI на проде нет, читаем через python из .venv
DB_PATH="${MVP_SQLITE_DB_PATH:-$REPO_DIR/data/portfolio.db}"
TG_CREDS="$("$REPO_DIR/.venv/bin/python3" -c "
import sqlite3
s = dict(sqlite3.connect('$DB_PATH').execute(
    \"SELECT key, value FROM app_settings WHERE key IN ('tg_bot_token','tg_chat_id')\").fetchall())
print(s.get('tg_bot_token', ''))
print(s.get('tg_chat_id', ''))
" 2>/dev/null || true)"
TG_TOKEN="$(printf '%s\n' "$TG_CREDS" | sed -n 1p)"
TG_CHAT="$(printf '%s\n' "$TG_CREDS" | sed -n 2p)"
[ -z "$TG_TOKEN" ] && TG_TOKEN="${MVP_TG_BOT_TOKEN:-}"

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

# Каким путём перезапустились воркеры. При успехе вывод deploy.sh в лог не
# попадает, а путь важен: graceful не перечитывает .env, и по логу должно быть
# видно, был ли простой. Фолбэк "?" — если deploy.sh промолчал (нечего катить).
if printf '%s' "$DEPLOY_LOG" | grep -q "SIGHUP не доставлен"; then
  RESTART_MODE="полный restart (SIGHUP не доставлен)"
elif printf '%s' "$DEPLOY_LOG" | grep -q "graceful-reload"; then
  RESTART_MODE="graceful-reload (SIGHUP, без простоя)"
elif printf '%s' "$DEPLOY_LOG" | grep -q "restart bondai (полный)"; then
  RESTART_MODE="полный restart"
else
  RESTART_MODE="?"
fi

if [ "$DEPLOY_OK" = "1" ]; then
  _log "✓ авто-деплой ок: $SHORT_OLD → $SHORT_NEW (+$BEHIND коммит(ов)), рестарт: $RESTART_MODE"
  _tg "✅ <b>bondai авто-деплой</b>%0A%2B$BEHIND коммит(ов): <code>$SHORT_OLD → $SHORT_NEW</code>%0A$RESTART_MODE"
else
  _log "✗ авто-деплой ПРОВАЛИЛСЯ (deploy.sh вернул ненулевой код)"
  _log "$DEPLOY_LOG"
  _tg "❌ <b>bondai авто-деплой ПРОВАЛИЛСЯ</b>%0A<code>$SHORT_OLD → $SHORT_NEW</code>%0AПроверь journalctl -u bondai"
  exit 1
fi
