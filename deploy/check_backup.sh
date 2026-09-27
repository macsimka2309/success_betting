#!/bin/bash
# Проверка бэкапа и диска (docs/04, расхождение 4). Только чтение, ничего не
# меняет и не расходует лимит API. Код возврата: 0 — всё в порядке, 1 — есть
# проблема (свежая логика cron уже поддерживает такой контракт, см. run_collect.sh).
#
#   check_backup.sh <каталог_бэкапов> <мин_размер_байт> <мин_своб_ГБ>
set -uo pipefail

BACKUP_DIR="${1:?укажите каталог бэкапов}"
MIN_SIZE="${2:-1000000}"       # 1 МБ: пустая база даёт дамп ~800 байт
MIN_FREE_GB="${3:-10}"         # порог из docs/04

LOG=/opt/apps/football-collector/logs/check-backup.log
ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { echo "[$(ts)] $*" >> "$LOG"; }

problems=0

latest=$(ls -1 "$BACKUP_DIR"/football_*.dump 2>/dev/null | sort | tail -1)
if [ -z "$latest" ]; then
    log "ПРОБЛЕМА: в $BACKUP_DIR не найдено ни одного дампа"
    problems=1
else
    age_hours=$(( ($(date +%s) - $(stat -c %Y "$latest")) / 3600 ))
    size=$(stat -c %s "$latest")
    if [ "$age_hours" -gt 26 ]; then
        log "ПРОБЛЕМА: последний дамп $latest старше 26 часов ($age_hours ч)"
        problems=1
    fi
    if [ "$size" -lt "$MIN_SIZE" ]; then
        log "ПРОБЛЕМА: последний дамп $latest весит $size байт (< $MIN_SIZE) — похоже, база пуста или бэкап сломан"
        problems=1
    fi
    [ "$problems" -eq 0 ] && log "дамп в порядке: $latest, возраст ${age_hours} ч, размер $size байт"
fi

free_gb=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
if [ "$free_gb" -lt "$MIN_FREE_GB" ]; then
    log "ПРОБЛЕМА: свободно на диске ${free_gb} ГБ (< ${MIN_FREE_GB} ГБ)"
    problems=1
else
    log "места на диске достаточно: ${free_gb} ГБ свободно"
fi

exit $problems
