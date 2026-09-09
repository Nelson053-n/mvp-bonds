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
# Только при успешном импорте перезапускаем воркеры + health-curl.
#
# Зависимости из requirements.txt ставятся автоматически, но только когда сам
# файл изменился в этом деплое. После установки перезапуск принудительно
# полный: SIGHUP переиспользует родительский процесс и новый пакет не увидит.
#
# Перезапуск по умолчанию graceful (SIGHUP, без простоя). Полный рестарт нужен
# при смене .env или юнита — SIGHUP их не перечитывает:
#   DEPLOY_FORCE_RESTART=1 ops/deploy.sh   (или systemctl restart bondai)
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

# Зависимости ставим ДО smoke-импорта: пакет, добавленный в requirements.txt,
# иначе на прод не попадает вовсе. 09.09 так и вышло с reportlab — экспорт PDF
# отдавал 500, потому что деплой ставил только код. Smoke-гейт этого не ловит:
# reportlab импортируется внутри функции, а не на верхнем уровне модуля.
#
# Ставим только при изменившемся requirements.txt: обычный деплой не должен
# ходить в сеть на каждом коммите. При сбое установки откатываем pull и НЕ
# трогаем работающий сервис — как и при упавшем импорте.
if ! git diff --quiet "$PREV_HEAD" "$NEW_HEAD" -- requirements.txt; then
  echo "▶ requirements.txt изменился — ставлю зависимости…"
  if ! .venv/bin/pip install -q -r requirements.txt; then
    echo "✗ pip install УПАЛ — откатываю pull на $PREV_HEAD, сервис НЕ трогаю"
    git reset --hard "$PREV_HEAD"
    exit 1
  fi
  echo "✓ зависимости установлены"
  # Новый пакет виден только свежему процессу: SIGHUP переиспользует родителя
  # со старым sys.path и уже импортированными модулями.
  DEPS_CHANGED=1
else
  echo "▶ requirements.txt не менялся — установку пропускаю"
fi

echo "▶ smoke-импорт app.main (env из .env)…"
set -a; . ./.env; set +a
if ! .venv/bin/python3 -c 'import app.main' ; then
  echo "✗ ИМПОРТ УПАЛ — откатываю pull на $PREV_HEAD, сервис НЕ трогаю"
  git reset --hard "$PREV_HEAD"
  exit 1
fi
echo "✓ импорт ок"

# Перезапуск воркеров без простоя: uvicorn держит слушающий сокет в
# родительском процессе и по SIGHUP пересоздаёт воркеров по одному, не
# закрывая сокет — соединения ждут в backlog вместо Connection refused.
# `systemctl restart` убивал родителя вместе с сокетом: замер под нагрузкой
# 10.08 дал 46 отбитых запросов из 400 (~2.3с), при SIGHUP — 0 из 400.
#
# SIGHUP переиспользует родителя, поэтому НЕ перечитывает .env и не проходит
# ExecStartPre. Смену переменных окружения катить руками:
#   systemctl restart bondai
# Код подхватывается штатно — воркеры стартуют заново и импортируют его с нуля.
MAIN_PID="$(systemctl show bondai -p MainPID --value)"
if [ "${DEPLOY_FORCE_RESTART:-0}" = "1" ] || [ "${DEPS_CHANGED:-0}" = "1" ] \
   || [ -z "$MAIN_PID" ] || [ "$MAIN_PID" = "0" ]; then
  echo "▶ restart bondai (полный)…"
  systemctl restart bondai
else
  echo "▶ graceful-reload bondai (SIGHUP → $MAIN_PID)…"
  # Гонка: процесс мог умереть между чтением MainPID и сигналом. Без отката
  # set -e оборвал бы деплой на мёртвом сервисе (Restart=always его не
  # поднимет — для systemd юнит всё ещё «активен»).
  if ! kill -HUP "$MAIN_PID" 2>/dev/null; then
    echo "  SIGHUP не доставлен (процесс $MAIN_PID исчез) — полный restart"
    systemctl restart bondai
  fi
fi
sleep 6

# Health с ретраем: после SIGHUP воркеры перезапускаются по одному, и первый
# запрос может прийти раньше, чем последний из них поднялся.
echo "▶ health-check…"
code=000
for attempt in 1 2 3 4 5; do
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 http://127.0.0.1:8002/ || echo 000)"
  [ "$code" = "200" ] && break
  echo "  попытка $attempt: HTTP $code, жду 3с…"
  sleep 3
done
if [ "$code" != "200" ]; then
  echo "✗ health-check вернул $code (ожидался 200) — проверь journalctl -u bondai"
  exit 1
fi

# Health по / не трогает БД: это публичная страница. 27.08 сломанная миграция
# оставила код читать несуществующую колонку users.tg_chat_id_reset — импорт
# прошёл, health прошёл, а /auth/me три часа отдавал 500 (81 отказ).
#
# Дёргаем путь, который реально читает users, сервисным JWT. Без токена
# проверка бессмысленна: get_current_user отсекает запрос до похода в БД.
echo "▶ smoke-запрос к БД (/auth/me/portfolios-stats)…"
DB_PROBE="$(.venv/bin/python3 - <<'PYEOF' 2>&1
import sqlite3, urllib.error, urllib.request
from app.config import settings
from app.services.auth_service import auth_service

con = sqlite3.connect(f"file:{settings.sqlite_db_path}?mode=ro", uri=True)
row = con.execute("SELECT id, username FROM users ORDER BY id LIMIT 1").fetchone()
if not row:
    print("SKIP таблица users пуста")
    raise SystemExit
token = auth_service.create_token(row[0], row[1], False)
req = urllib.request.Request(
    "http://127.0.0.1:8002/auth/me/portfolios-stats",
    headers={"Authorization": f"Bearer {token}"},
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print(f"OK HTTP {resp.status}")
except urllib.error.HTTPError as exc:
    # 4xx — путь жив, отвечает осмысленно. 5xx — код и схема разошлись.
    print(f"{'FAIL' if exc.code >= 500 else 'OK'} HTTP {exc.code}")
except Exception as exc:
    print(f"FAIL {type(exc).__name__}: {exc}")
PYEOF
)"
echo "  $DB_PROBE"
case "$DB_PROBE" in
  OK*|SKIP*) ;;
  *)
    echo "✗ ПУТЬ К БД ОТВЕЧАЕТ ОШИБКОЙ — откатываю на $PREV_HEAD и перезапускаю"
    git reset --hard "$PREV_HEAD"
    systemctl restart bondai
    exit 1
    ;;
esac

echo "✓ деплой ок: HTTP $code, HEAD $NEW_HEAD"
