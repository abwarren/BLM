#!/usr/bin/env bash
# Prospective health snapshot scheduler (accumulation phase).
#
# Runs the READ-ONLY prospective health report once per day and APPENDS
# the snapshot to the APPEND-ONLY durable history:
#   /home/ubuntu/BLM/prospective_health_history.jsonl
# (override per-run with PROSPECTIVE_HISTORY=... if ever needed).
# Prior snapshots are never rewritten.  This is data-maturation
# monitoring only: no product behaviour, no analytical semantics, no
# optimisation.
#
# Schedule: cron entry, e.g. daily at 15:05 UTC
#   5 15 * * *  /home/ubuntu/BLM/scripts/prospective_health_daily.sh
# Install/refresh the cron entry idempotently with:
#   /home/ubuntu/BLM/scripts/prospective_health_daily.sh install-cron
set -u

HISTORY="${PROSPECTIVE_HISTORY:-/home/ubuntu/BLM/prospective_health_history.jsonl}"
LOG="${PROSPECTIVE_LOG:-/tmp/prospective_health_daily.log}"
REPO="/home/ubuntu/BLM"
CRON_LINE="5 15 * * * /home/ubuntu/BLM/scripts/prospective_health_daily.sh"
CRON_MARKER="prospective_health_daily.sh"
WINDOW_HOURS="${PROSPECTIVE_WINDOW_HOURS:-24}"

# Requirement 8: ensure the history's parent directory exists.
mkdir -p "$(dirname "$LOG")" "$(dirname "$HISTORY")"

# Upsert the cron line into a crontab content stream (pure: stdin →
# stdout).  Idempotent by construction — any previous line carrying the
# marker is removed first, so repeated installation yields exactly one
# entry.  Used by `install-cron`; unit-tested in
# tests/test_prospective_history_durability.py.
upsert_cron_line() {
  grep -vF "$CRON_MARKER" || true
  printf '%s\n' "$CRON_LINE"
}

do_snapshot() {
  # Requirement 7: the history file exists (empty is valid; snapshots
  # are appended ONLY on a successful report below).
  touch "$HISTORY"
  {
    echo "=== snapshot run $(date -u +%Y-%m-%dT%H:%M:%SZ) ==="
    cd "$REPO" || exit 1
    python3 -m blm_v4.live_analytics.prospective_health \
      --window-hours "$WINDOW_HOURS" \
      --api-url "http://127.0.0.1:8901" \
      --jsonl-append "$HISTORY" >/dev/null 2>>"$LOG"
    rc=$?
    if [ "$rc" -eq 0 ]; then
      echo "appended: $(wc -l < "$HISTORY") snapshots total at $HISTORY"
    else
      echo "SNAPSHOT FAILED rc=$rc (history left untouched — append-only preserved)"
    fi
  } >> "$LOG" 2>&1
}

do_install_cron() {
  "$(command -v crontab)" -l 2>/dev/null | upsert_cron_line | \
    "$(command -v crontab)" -
  echo "cron entry ensured (idempotent): $CRON_LINE"
}

# Execute only when run directly; sourcing (for tests) is side-effect free.
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  case "${1:-}" in
    install-cron) do_install_cron ;;
    *) do_snapshot ;;
  esac
fi
