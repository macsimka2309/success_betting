#!/bin/bash
# Обёртка запуска оповещений по идущим матчам из cron.
#   run_live_alerts.sh [--dry-run] [--max-requests N]
#
# - подхватывает .env (ключ API, база, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_PROXY);
# - не даёт запуститься второй копии: если прошлый запуск не завершён, новый пропускается;
# - пишет лог с отметками времени в UTC, токен в лог не попадает;
# - отказывается работать, если часовой пояс сервера не MSK (UTC+3), как run_collect.sh.
set -uo pipefail

ROOT=/opt/apps/football-collector
LOG="$ROOT/logs/live-alerts.log"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

if [ "$(date +%z)" != "+0300" ]; then
    log "ОТКАЗ: часовой пояс сервера $(date +%z), ожидается +0300 (MSK)."
    exit 1
fi

exec 9> "$ROOT/logs/.live-alerts.lock"
if ! flock -n 9; then
    log "пропуск: прошлый запуск ещё выполняется"
    exit 0
fi

cd "$ROOT" || exit 1
set -a; . ./.env; set +a

"$ROOT/.venv/bin/python" -m src.jobs.live_alerts --cache-dir "$ROOT/cache" "$@" >> "$LOG" 2>&1
code=$?
[ "$code" -ne 0 ] && log "код выхода $code"
exit $code
