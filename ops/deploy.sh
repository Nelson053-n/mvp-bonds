#!/usr/bin/env bash
# Атомарный деплой bondai с smoke-гейтом.
#
# Корень инцидента 23.06 (502): на прод уехал код, который при импорте требовал
# файл, которого не было в git (app/ui/static/translations.js). `git pull` +
# `systemctl restart` слепо перезапустили мёртвое приложение → 502, при этом
# systemctl показывал active (Restart=always крутил труп).
#
# Гейт: после git pull проверяем `python -c 'import app.main'`. Если импорт
# падает — ОТКАТЫВАЕМ pull на прежний HEAD и НЕ трогаем работающий сервис.
# Только при успешном импорте делаем restart + health-curl.
#
# Запуск на проде: cd /opt/mvp-bonds && ops/deploy.sh
set -euo pipefail

cd /opt/mvp-bonds

PREV_HEAD="$(git rev-parse HEAD)"
echo "▶ текущий HEAD: $PREV_HEAD"

echo "▶ git pull…"
git pull --ff-only

NEW_HEAD="$(git rev-parse HEAD)"
if [ "$NEW_HEAD" = "$PREV_HEAD" ]; then
  echo "✓ нечего деплоить (HEAD не изменился), рестарт не нужен"
  exit 0
fi
echo "▶ новый HEAD: $NEW_HEAD"

echo "▶ smoke-импорт app.main (env из .env)…"
set -a; . ./.env; set +a
if ! .venv/bin/python3 -c 'import app.main' ; then
  echo "✗ ИМПОРТ УПАЛ — откатываю pull на $PREV_HEAD, сервис НЕ трогаю"
  git reset --hard "$PREV_HEAD"
  exit 1
fi
echo "✓ импорт ок"

echo "▶ restart bondai…"
systemctl restart bondai
sleep 4

echo "▶ health-check…"
code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:8002/ || echo 000)"
if [ "$code" != "200" ]; then
  echo "✗ health-check вернул $code (ожидался 200) — проверь journalctl -u bondai"
  exit 1
fi
echo "✓ деплой ок: HTTP $code, HEAD $NEW_HEAD"
