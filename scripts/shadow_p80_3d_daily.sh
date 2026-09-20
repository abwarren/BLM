#!/usr/bin/env bash
# Prospective collector for the FROZEN 3-day trailing P80 shadow candidate.
#
# READ-ONLY with respect to production.  The DBs are opened mode=ro by the
# Python monitor; this wrapper only APPENDS to the separate analysis dataset:
#   /home/ubuntu/BLM/shadow_p80_3d_triggers.jsonl
# Prior records are never rewritten (append-only; settlement updates are
# appended as new dedup-marked lines).
#
# This is prospective ACCUMULATION ONLY.  Parameters are frozen:
#   3-day window, P80, minimum 100 calibration observations.
# Nothing here optimises, re-tunes, or feeds back into production alerts.
#
# Schedule: cron entry, daily at 15:35 UTC
#   35 15 * * * /home/ubuntu/BLM/scripts/shadow_p80_3d_daily.sh
# Install/refresh idempotently with:
#   /home/ubuntu/BLM/scripts/shadow_p80_3d_daily.sh install-cron
# Remove with:
#   /home/ubuntu/BLM/scripts/shadow_p80_3d_daily.sh uninstall-cron
set -u

REPO="/home/ubuntu/BLM"
DATASET="${SHADOW_DATASET:-/home/ubuntu/BLM/shadow_p80_3d_triggers.jsonl}"
LOG="${SHADOW_LOG:-/tmp/shadow_p80_3d_daily.log}"
CRON_LINE="35 15 * * * /home/ubuntu/BLM/scripts/shadow_p80_3d_daily.sh"
CRON_MARKER="shadow_p80_3d_daily.sh"

mkdir -p "$(dirname "$LOG")" "$(dirname "$DATASET")"

upsert_cron_line() {          # pure: stdin -> stdout, idempotent
  grep -vF "$CRON_MARKER" || true
  printf '%s\n' "$CRON_LINE"
}

do_report() {
  touch "$DATASET"
  {
    echo "=== shadow run $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    cd "$REPO" || exit 1
    python3 "$REPO/scripts/shadow_p80_3d.py" \
      --jsonl-append "$DATASET" --quiet
    rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "ok: $(grep -c . "$DATASET" 2>/dev/null || echo 0) records in dataset"
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
    *) do_report ;;
  esac
fi
