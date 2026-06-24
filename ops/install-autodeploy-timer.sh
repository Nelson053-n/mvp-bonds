#!/usr/bin/env bash
# Устанавливает systemd timer для авто-деплоя bondai.
# Запускать один раз на проде от root:
#   ssh root@212.8.228.248 "cd /opt/mvp-bonds && bash ops/install-autodeploy-timer.sh"

set -euo pipefail

REPO_DIR="/opt/mvp-bonds"
SYSTEMD_DIR="/etc/systemd/system"

echo "▶ копируем юниты в $SYSTEMD_DIR…"
cp "$REPO_DIR/ops/systemd/bondai-autodeploy.service" "$SYSTEMD_DIR/"
cp "$REPO_DIR/ops/systemd/bondai-autodeploy.timer"   "$SYSTEMD_DIR/"

chmod +x "$REPO_DIR/ops/auto-deploy.sh"

echo "▶ reload systemd + enable + start timer…"
systemctl daemon-reload
systemctl enable --now bondai-autodeploy.timer

echo "▶ статус timer'а:"
systemctl status bondai-autodeploy.timer --no-pager

echo ""
echo "✓ авто-деплой активирован."
echo "  Следующие запуски: systemctl list-timers bondai-autodeploy.timer"
echo "  Логи авто-деплоя: journalctl -u bondai-autodeploy.service -f"
echo "  Файл лога:        tail -f /var/log/bondai-autodeploy.log"
