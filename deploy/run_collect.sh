#!/bin/bash
# Обёртка запуска сбора из cron.
#   run_collect.sh <шаг> [макс. запросов]
#
# - подхватывает .env (ключ API и строка подключения к базе);
# - не даёт запуститься второй копии того же шага (flock): если вчерашний
#   ежедневный сбор ещё идёт, новый молча пропускается;
# - пишет лог с отметками времени в UTC;
# - отказывается работать, если часовой пояс сервера не MSK (UTC+3):
#   расписание в /etc/cron.d/football-collector пересчитано из UTC под него,
#   и при смене пояса запуски сместились бы незаметно.
set -uo pipefail

JOB="${1:?укажите шаг: catalog|odds|daily|cleanup|...}"
MAX_REQUESTS="${2:-}"
ROOT=/opt/apps/football-collector
LOG="$ROOT/logs/collect-$JOB.log"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

if [ "$(date +%z)" != "+0300" ]; then
    log "ОТКАЗ: часовой пояс сервера $(date +%z), ожидается +0300 (MSK). Расписание рассчитано под MSK."
    exit 1
fi

exec 9> "$ROOT/logs/.$JOB.lock"
if ! flock -n 9; then
    log "пропуск: шаг $JOB ещё выполняется"
    exit 0
fi

cd "$ROOT" || exit 1
set -a; . ./.env; set +a

ARGS=(--job "$JOB" --cache-dir "$ROOT/cache")
[ -n "$MAX_REQUESTS" ] && ARGS+=(--max-requests "$MAX_REQUESTS")

log "старт $JOB ${MAX_REQUESTS:+(бюджет $MAX_REQUESTS)}"
"$ROOT/.venv/bin/python" -m src.jobs.collect "${ARGS[@]}" >> "$LOG" 2>&1
code=$?
log "конец $JOB, код выхода $code"
exit $code
