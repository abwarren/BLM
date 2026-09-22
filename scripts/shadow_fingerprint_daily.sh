#!/usr/bin/env bash
# Daily READ-ONLY shadow fingerprint collector (75% UNDER-alert cohort).
#
# The Python collector opens both production DBs mode=ro + query_only;
# this wrapper only APPENDS to the separate analysis dataset:
#   /home/ubuntu/BLM/shadow_fingerprints_75.jsonl
# Append-only + dedup by (game_id, captured_at); settlement updates are
# appended as dedup-marked lines.  Prior records are never rewritten.
#
# Nothing here changes production alerts, thresholds or services.  The
# production gates (mode=ro, query_only, separate file, observation-only
# rules) are frozen in the Python collector itself.
#
# Schedule: cron entry, daily at 15:45 UTC (after the shadow_p80_3d run).
#   45 15 * * * /home/ubuntu/BLM/scripts/shadow_fingerprint_daily.sh
# Install/refresh idempotently with:
#   /home/ubuntu/BLM/scripts/shadow_fingerprint_daily.sh install-cron
# Remove with:
#   /home/ubuntu/BLM/scripts/shadow_fingerprint_daily.sh uninstall-cron
set -u

REPO="/home/ubuntu/BLM"
PY="${REPO}/venv/bin/python"
COLLECTOR="${REPO}/scripts/shadow_fingerprint_collector_2026-09-21.py"
DATASET="${SHADOW_DATASET:-${REPO}/shadow_fingerprints_75.jsonl}"
LOG="${SHADOW_LOG:-/tmp/shadow_fingerprint_daily.log}"
CRON_LINE="45 15 * * * ${REPO}/scripts/shadow_fingerprint_daily.sh"
CRON_MARKER="shadow_fingerprint_daily.sh"

mkdir -p "$(dirname "$LOG")" "$(dirname "$DATASET")"

upsert_cron_line() {          # pure: stdin -> stdout, idempotent
  grep -vF "$CRON_MARKER" || true
  printf '%s\n' "$CRON_LINE"
}

do_run() {
  touch "$DATASET"
  {
    echo "=== shadow fingerprint run $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    cd "$REPO" || exit 1
    "$PY" "$COLLECTOR" --run --report --log "$DATASET" --quiet
    rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "ok: $(grep -c . "$DATASET" 2>/dev/null || echo 0) lines in dataset"
    else
      echo "FAILED rc=$rc (dataset left untouched)"
    fi
  } >> "$LOG" 2>&1
}

do_install_cron() {
  "$(command -v crontab)" -l 2>/dev/null | upsert_cron_line | \
    "$(command -v crontab)" -
  echo "cron entry ensured (idempotent): $CRON_LINE"
}

do_uninstall_cron() {
  "$(command -v crontab)" -l 2>/dev/null | grep -vF "$CRON_MARKER" | \
    "$(command -v crontab)" - || true
  echo "cron entry removed (marker: $CRON_MARKER)"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${1:-}" in
    install-cron) do_install_cron ;;
    uninstall-cron) do_uninstall_cron ;;
    *) do_run ;;
  esac
fi
