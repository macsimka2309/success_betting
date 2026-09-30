#!/bin/bash
# Обёртка запуска препроцессинга (src.jobs.build_features) из cron.
#   run_build_features.sh <режим>   режим: upcoming|incremental|full-rebuild
#
# - подхватывает .env (строка подключения к базе);
# - не даёт запуститься второй копии того же режима (flock);
# - пишет лог с отметками времени в UTC.
#
# Пояс сервера тут не проверяем (в отличие от run_collect.sh): build_features
# не зависит от расписания дня, только от текущего момента (`now()` в SQL).
set -uo pipefail

MODE="${1:?укажите режим: upcoming|incremental|full-rebuild}"
ROOT=/opt/apps/football-collector
LOG="$ROOT/logs/build_features-$MODE.log"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

exec 9> "$ROOT/logs/.build_features_$MODE.lock"
if ! flock -n 9; then
    log "пропуск: режим $MODE ещё выполняется"
    exit 0
fi

cd "$ROOT" || exit 1
set -a; . ./.env; set +a

case "$MODE" in
    upcoming)     ARGS=(--upcoming) ;;
    incremental)  ARGS=(--incremental) ;;
    full-rebuild) ARGS=() ;;
    *) log "ОТКАЗ: неизвестный режим $MODE"; exit 1 ;;
esac

log "старт build_features $MODE"
"$ROOT/.venv/bin/python" -m src.jobs.build_features "${ARGS[@]}" >> "$LOG" 2>&1
code=$?
log "конец $MODE, код выхода $code"
exit $code
