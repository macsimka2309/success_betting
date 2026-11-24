#!/bin/bash
# Sports-only read. No .env/profile reads, DB writes or browser restarts.
set -euo pipefail
ROOT=${FONBET_LIVE_ROOT:-/opt/apps/fonbet-sync-pilot}
exec 9>"$ROOT/output/live/collect.lock"
flock -n -E 75 9 || exit 75
python3 "$ROOT/browser_lock.py" --root "$ROOT" --mode live -- sudo -n docker exec fonbet-browser-pilot flock -n -E 75 /profile/fonbet-browser.lock flock -n -E 75 /profile/fonbet-live.lock node /work/live/timeline.cjs "$1" "${2:-}" "${3:-live}"
