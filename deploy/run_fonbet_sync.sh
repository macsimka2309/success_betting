#!/bin/bash
# No .env sourcing; uses existing local PostgreSQL container credentials internally.
set -euo pipefail
umask 077
ROOT=${FONBET_SYNC_ROOT:-/opt/apps/fonbet-sync-pilot}
cd "$ROOT"
mkdir -p output
exec 9>output/sync.lock
flock -n 9 || exit 0
trap 'code=$?; if [ "$code" -ne 0 ]; then printf "%s failed exit=%s\n" "$(date -Is)" "$code" > output/last-error.txt; fi' EXIT
DAY=${1:-$(TZ=Europe/Moscow date -d yesterday +%F)}
DATES=$(python3 import_history.py --day "$DAY" --dates)
sudo -n docker start fonbet-browser-pilot >/dev/null
# export.cjs exits on missing authorization or incomplete traversal; no partial writes.
python3 "$ROOT/browser_lock.py" --root "$ROOT" --mode history -- sudo -n docker exec fonbet-browser-pilot flock -n -E 75 /profile/fonbet-browser.lock sh -c 'node /work/live/browser_probe.cjs >/dev/null && exec node /work/export.cjs "$1" /profile/history-export.json' _ "$DATES"
sudo -n docker exec fonbet-browser-pilot cat /profile/history-export.json > "output/history-$DAY.json.tmp"
mv "output/history-$DAY.json.tmp" "output/history-$DAY.json"
set +e
python3 import_history.py --day "$DAY" --input "output/history-$DAY.json" --apply > output/last-result.json.tmp
CODE=$?
set -e
if [[ "$CODE" != 0 && "$CODE" != 2 ]]; then rm -f output/last-result.json.tmp; exit "$CODE"; fi
mv output/last-result.json.tmp output/last-result.json
cat output/last-result.json
if [[ "$CODE" == 2 ]]; then exit 2; fi
printf '%s success\n' "$(date -Is)" > output/last-success.txt
