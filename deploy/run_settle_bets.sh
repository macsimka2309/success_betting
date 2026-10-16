#!/bin/bash
# Обёртка запуска расчёта ставок из cron (specs/реестр-ставок.md).
#   run_settle_bets.sh [--max-requests N]
# Те же правила, что у run_live_alerts.sh: .env, flock, лог в UTC, отказ при смене пояса.
set -uo pipefail

ROOT=/opt/apps/football-collector
LOG="$ROOT/logs/settle-bets.log"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

if [ "$(date +%z)" != "+0300" ]; then
    log "ОТКАЗ: часовой пояс сервера $(date +%z), ожидается +0300 (MSK)."
    exit 1
fi

exec 9> "$ROOT/logs/.settle-bets.lock"
if ! flock -n 9; then
    log "пропуск: прошлый запуск ещё выполняется"
    exit 0
fi

cd "$ROOT" || exit 1
set -a; . ./.env; set +a

"$ROOT/.venv/bin/python" -m src.jobs.settle_bets --cache-dir "$ROOT/cache" "$@" >> "$LOG" 2>&1
code=$?
[ "$code" -ne 0 ] && log "код выхода $code"
exit $code
