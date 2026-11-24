#!/bin/bash
# All regular/manual collection starts go through this wrapper.
set -euo pipefail
umask 077
ROOT=${FONBET_LIVE_ROOT:-/opt/apps/fonbet-sync-pilot}
CONTAINER=${FONBET_BROWSER_CONTAINER:-fonbet-browser-pilot}
MODE=${1:---dry-run}
if [[ "$MODE" != '--dry-run' && "$MODE" != '--apply' ]]; then
    echo 'Usage: run_fonbet_live.sh [--dry-run|--apply]' >&2; exit 2
fi
mkdir -p "$ROOT/output/live"
exec 9>"$ROOT/output/live/collect.lock"
if ! flock -n 9; then
    printf '{"status":"skipped_previous_running"}\n'
    exit 0
fi
# The same descriptor stays locked through browser read and DB storage. No queue.
TMPFILE=$(mktemp "$ROOT/output/live/run.XXXXXXXX.json")
trap 'rm -f "$TMPFILE"' EXIT
set +e
python3 "$ROOT/browser_lock.py" --root "$ROOT" --mode live -- sudo -n docker exec "$CONTAINER" flock -n -E 75 /profile/fonbet-browser.lock flock -n -E 75 /profile/fonbet-live.lock sh -c 'node /work/live/browser_probe.cjs >/dev/null && exec node /work/live/collect.cjs /profile/fonbet-live.json'
CODE=$?
set -e
# Failed browser exports are still useful diagnostics, but stale exports are not read.
if [[ "$CODE" == 75 ]]; then
    echo '{"status":"skipped_browser_busy"}'; exit 0
fi
if [[ "$CODE" != 0 ]]; then
    echo '{"status":"failed","reason":"browser_collection_failed"}' >&2
    exit "$CODE"
fi
sudo -n docker exec "$CONTAINER" cat /profile/fonbet-live.json > "$TMPFILE"
if [[ "$MODE" == '--apply' ]]; then
    python3 "$ROOT/live/store.py" --input "$TMPFILE" --apply
else
    python3 "$ROOT/live/store.py" --input "$TMPFILE"
fi
mv "$TMPFILE" "$ROOT/output/live/last-export.json"
if [[ "$MODE" == '--apply' ]]; then
    python3 "$ROOT/live/reconcile.py" --input "$ROOT/output/live/last-export.json" --apply
fi
