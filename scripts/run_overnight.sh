#!/usr/bin/env bash
# Runs the remaining experiment plans one after the other (never in parallel), seeds 1,2,3, speed from each plan (15).
# Usage, from anywhere:   bash scripts/run_overnight.sh
# Results: experiments/overnight_<date-time>/<plan name>/   and a log per plan in the same folder.
# A plan that fails does not stop the next ones. Safe to stop with Ctrl+C (finished plans keep their files).

cd "$(dirname "$0")/.." || exit 1

PLANS=(
  03_burst_one_hub_brains
  04_centre_drivers_suburb_requests_brains
  05_antecipated_surge_reposition_brains
  06_hub_failure_brains
  07_brains
  08_full_8hubs
)
SEEDS="1,2,3"
ROOT="experiments/overnight_$(date +%Y%m%d_%H%M%S)"
mkdir -p "$ROOT"

# keep the computer awake while this runs (if systemd-inhibit exists)
if [ -z "$INHIBITED" ] && command -v systemd-inhibit >/dev/null 2>&1; then
  export INHIBITED=1
  exec systemd-inhibit --what=sleep:idle --why="experiments" bash "$0" "$@"
fi

# make sure no leftover XMPP server/simulation from an earlier run is holding the port
if pgrep -f "spade.cli run" >/dev/null 2>&1; then
  echo "WARNING: a spade server is already running (pgrep -f 'spade.cli run'); stop it first or results may be wrong." | tee -a "$ROOT/summary.log"
fi

echo "start $(date)  ->  $ROOT" | tee -a "$ROOT/summary.log"
for p in "${PLANS[@]}"; do
  echo "[$(date +%H:%M:%S)] $p ..." | tee -a "$ROOT/summary.log"
  uv run python -m harness.experiment --plan "config/experiments/$p.json" --seeds "$SEEDS" \
      --out "$ROOT/$p" > "$ROOT/$p.log" 2>&1
  code=$?
  echo "[$(date +%H:%M:%S)] $p finished with exit code $code" | tee -a "$ROOT/summary.log"
  sleep 20      # let the XMPP server and ports close before the next plan
done
echo "done $(date)" | tee -a "$ROOT/summary.log"
