#!/bin/bash
# Обёртка запуска баскетбольного сбора из cron.
#   run_basketball.sh <шаг> [макс. запросов] [доп. аргументы]
# шаги: catalog | games | stats | odds | report
#
# - подхватывает .env (ключ API и строка подключения к базе);
# - не даёт запуститься второй копии того же шага (flock);
# - пишет лог с отметками времени в UTC;
# - отказывается работать, если часовой пояс сервера не MSK (расписание под MSK).
set -uo pipefail

JOB="${1:?укажите шаг: catalog|games|stats|odds|report}"
MAX_REQUESTS="${2:-}"
shift $(( $# > 1 ? 2 : 1 ))
ROOT=/opt/apps/football-collector
LOG="$ROOT/logs/basketball-$JOB.log"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

if [ "$(date +%z)" != "+0300" ]; then
    log "ОТКАЗ: часовой пояс сервера $(date +%z), ожидается +0300 (MSK)."
    exit 1
fi

exec 9> "$ROOT/logs/.basketball-$JOB.lock"
if ! flock -n 9; then
    log "пропуск: шаг $JOB ещё выполняется"
    exit 0
fi

cd "$ROOT" || exit 1
set -a; . ./.env; set +a

ARGS=(--job "$JOB" --cache-dir "$ROOT/cache_bb")
[ -n "$MAX_REQUESTS" ] && ARGS+=(--max-requests "$MAX_REQUESTS")

log "старт $JOB ${MAX_REQUESTS:+(бюджет $MAX_REQUESTS)} $*"
"$ROOT/.venv/bin/python" -m src.jobs.basketball "${ARGS[@]}" "$@" >> "$LOG" 2>&1
code=$?
log "конец $JOB, код выхода $code"
exit $code
