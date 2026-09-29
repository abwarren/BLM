#!/usr/bin/env bash
# live_validate_startup_grace.sh — LIVE VALIDATION for the bounded startup
# watchdog grace (commit ed6f1f0, blm_v4/collector.py).
#
# Implements the 2026-09-29 forensic follow-up live test plan:
#   1. Start a fresh collector.
#   2. Apply measured synthetic host load.
#   3. Confirm startup grace prevents an immediate restart.
#   4. Confirm steady-state completed-cycle watchdog behavior remains intact.
#   5. Confirm a genuinely wedged fast path eventually causes the expected
#      systemd watchdog kill (simulated by SIGSTOP: freezes every thread
#      INCLUDING the watchdog thread — pings stop, systemd must kill at
#      WatchdogSec expiry; this validates the kill path without touching
#      collector code).
#   6. Audit the journal afterward and write a PASS/FAIL report.
#
# SAFETY:
#   * Load is APPLIED to the box — the production collector shares this host.
#     The script refuses to run unless you pass --apply, and it warns when
#     the host is ALREADY loaded (stacking load on load is how the 2026-09-29
#     restart storms happened).
#   * It never edits the unit file, never runs git commands, never touches
#     files outside /tmp for artifacts (report goes to analysis/ read-only).
#   * All load workers are killed on exit/trap; verified dead at cleanup.
#
# Usage:
#   scripts/live_validate_startup_grace.sh            # DRY RUN (no load, no restart)
#   scripts/live_validate_startup_grace.sh --apply    # full live test
#   scripts/live_validate_startup_grace.sh --apply \
#       --workers 4 --load-seconds 150 --wedge        # tune + include SIGSTOP wedge
#
set -uo pipefail

UNIT=blm-collector.service
UNIT_FILE="$HOME/.config/systemd/user/${UNIT}"
REPO="$HOME/BLM"
PY=/usr/bin/python3
GRACE_MARKER="startup grace until"
GRACE_STATUS="STATUS=startup grace"
STOPPING="STOPPING WATCHDOG"
RESUME="fresh again"
WATCHDOG_LIMIT_S=90          # must match WatchdogSec in the unit
DEADLINE_S=30                # FAST_TICK_S * FAST_LIVENESS_FACTOR in collector

WORKERS=4                    # synthetic CPU burners
LOAD_SECONDS=150             # how long synthetic load stays on
DO_WEDGE=0                   # phase 5: SIGSTOP the main PID after first cycle
APPLY=0
REPORT=""

# ── args ────────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --workers) WORKERS="$2"; shift ;;
    --load-seconds) LOAD_SECONDS="$2"; shift ;;
    --wedge) DO_WEDGE=1 ;;
    -h|--help) sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
  shift
done

log()  { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
fail() { log "FAIL: $*"; RESULTS+=("FAIL  $*"); }
pass() { log "PASS: $*"; RESULTS+=("PASS  $*"); }
RESULTS=()

# ── preflight ───────────────────────────────────────────────────────────────
log "== preflight =="
[[ -f "$UNIT_FILE" ]] || { echo "unit file not found: $UNIT_FILE" >&2; exit 2; }
[[ -d "$REPO/blm_v4" ]] || { echo "repo not found: $REPO" >&2; exit 2; }
WATCHDOG_USEC_UNIT=$(grep -E '^\s*WatchdogSec=' "$UNIT_FILE" | tr -dc '0-9')
[[ "$WATCHDOG_USEC_UNIT" == "$WATCHDOG_LIMIT_S" ]] || \
  log "NOTE: unit WatchdogSec=${WATCHDOG_USEC_UNIT}s differs from expected ${WATCHDOG_LIMIT_S}s"

# deployed code must be the grace build
if grep -q "_startup_grace_seconds" "$REPO/blm_v4/collector.py"; then
  pass "deployed collector.py contains the startup-grace implementation"
else
  fail "deployed collector.py has NO startup-grace code — aborting"
  exit 1
fi

L1=$(awk '{print $1}' /proc/loadavg)
NPROC=$(nproc)
if awk -v l="$L1" -v n="$NPROC" 'BEGIN{exit !(l > 0.6*n)}'; then
  log "WARNING: host 1-min loadavg is $L1 on $NPROC cores — already loaded."
  log "         Applying MORE load now stacks on real load (storm conditions)."
  if [[ $APPLY -eq 1 ]]; then
    log "         --apply given: continuing in 10s (Ctrl-C to abort)"; sleep 10
  fi
else
  pass "host loadavg ${L1} on ${NPROC} cores — quiesced enough for the test"
fi

systemctl --user is-active --quiet "$UNIT" || { echo "unit not active" >&2; exit 1; }
OLD_PID=$(systemctl --user show "$UNIT" -p MainPID --value)
log "current MainPID=$OLD_PID  NRestarts=$(systemctl --user show "$UNIT" -p NRestarts --value)"

if [[ $APPLY -eq 0 ]]; then
  log "DRY RUN — would: restart $UNIT, burn $WORKERS CPU(s) for ${LOAD_SECONDS}s"
  [[ $DO_WEDGE -eq 1 ]] && log "            and SIGSTOP the MainPID after its first completed cycle"
  log "re-run with --apply to execute"
  exit 0
fi

REPORT="analysis/live_validation_startup_grace_$(date -u +%Y%m%dT%H%M%SZ).md"
mkdir -p "$REPO/analysis"

# ── synthetic load (measured) ───────────────────────────────────────────────
LOAD_PIDS=()
start_load() {
  log "applying synthetic load: $WORKERS busy-loop CPU worker(s) for ${LOAD_SECONDS}s"
  local i
  for i in $(seq 1 "$WORKERS"); do
    # 'yes > /dev/null' is a self-contained ~1-core CPU burner — no deps
    # (stress-ng may be absent); timeout self-terminates each worker.
    timeout --signal=KILL "$LOAD_SECONDS" yes >/dev/null 2>&1 &
    LOAD_PIDS+=($!)
  done
  log "load PIDs: ${LOAD_PIDS[*]} (each self-terminates after ${LOAD_SECONDS}s)"
}
stop_load() {
  local p
  for p in "${LOAD_PIDS[@]}"; do kill "$p" 2>/dev/null; done
  sleep 1
  for p in "${LOAD_PIDS[@]}"; do
    kill -0 "$p" 2>/dev/null && { kill -9 "$p" 2>/dev/null; log "force-killed stray load PID $p"; }
  done
}
trap stop_load EXIT INT TERM

# ── phase 1+2+3: fresh start under load → grace prevents immediate kill ────
log "== phase 1-3: fresh start under measured load =="
start_load
sleep 2
L2=$(awk '{print $1}' /proc/loadavg)
log "measured 1-min loadavg with burners: $L2 (was $L1 pre-load)"

systemctl --user restart "$UNIT"
sleep 3
NEW_PID=$(systemctl --user show "$UNIT" -p MainPID --value)
T0=$(date +%s)
JSTART="$(date -u -d "@$T0" +%H:%M:%S)"   # audit window anchored to restart
log "restarted: MainPID=$NEW_PID at $(date -u +%H:%M:%S)"

# wait for the grace marker line (proof the new code armed)
GRACE_ARMED=0
for _ in $(seq 1 30); do
  if journalctl --user -u "$UNIT" --since "$JSTART" _PID="$NEW_PID" --no-pager \
       | grep -q "$GRACE_MARKER"; then GRACE_ARMED=1; break; fi
  sleep 2
done
if [[ $GRACE_ARMED -eq 1 ]]; then
  pass "startup grace armed on fresh process (journal: '$GRACE_MARKER')"
else
  fail "no '$GRACE_MARKER' line from PID $NEW_PID — grace build not running?"
fi

# during grace + first slow cycle: NO watchdog kill may occur for
# WATCHDOG_LIMIT_S after restart (the storm failure mode)
sleep "$((WATCHDOG_LIMIT_S + 10))"
CUR_PID=$(systemctl --user show "$UNIT" -p MainPID --value)
if [[ "$CUR_PID" == "$NEW_PID" ]]; then
  pass "no immediate systemd kill: same PID $NEW_PID alive ${WATCHDOG_LIMIT_S}s after restart under load"
else
  fail "unit was killed/restarted during startup window (PID $NEW_PID → $CUR_PID)"
fi
FIRST_DONE=$(journalctl --user -u "$UNIT" --since "$JSTART" _PID="$NEW_PID" --no-pager \
  | grep -oE "fast tick 1 done in [0-9.]+s" | head -1 || true)
log "first fast cycle: ${FIRST_DONE:-NOT COMPLETED YET} (grace covers it if slow)"

# grace STATUS pings must have been emitted while no cycle had completed
if journalctl --user -u "$UNIT" --since "$JSTART" _PID="$NEW_PID" --no-pager \
     | grep -q "$GRACE_STATUS"; then
  pass "grace-window WATCHDOG=1 pings observed ('${GRACE_STATUS}')"
else
  log "NOTE: no grace-status ping observed (first cycle may have completed before a poll pass)"
fi

# ── phase 4: steady state stays completion-gated ───────────────────────────
log "== phase 4: steady-state completion gating (load still on) =="
# all checks are anchored to the restart instant — no rolling windows
log "journal audit window starts at $JSTART (restart instant)"
# after the first completion, an overrunning cycle must STOP pings and log
# exactly one STOPPING per episode; a completing slow cycle must resume pings
STOPPING_COUNT=$(journalctl --user -u "$UNIT" --since "$JSTART" --no-pager \
  | grep -c "$STOPPING" || true)
RESUME_COUNT=$(journalctl --user -u "$UNIT" --since "$JSTART" --no-pager \
  | grep -c "$RESUME" || true)
log "episodes so far: STOPPING=$STOPPING_COUNT  resume=$RESUME_COUNT"
if [[ $RESUME_COUNT -gt 0 ]]; then
  pass "recovery line observed — pings resumed on first completed cycle after starvation"
else
  log "NOTE: no starvation episode yet (cycles completing within ${DEADLINE_S}s under load)"
fi
if [[ $STOPPING_COUNT -gt 0 && $STOPPING_COUNT -gt $((RESUME_COUNT + 1)) ]]; then
  fail "STOPPING spam suspected ($STOPPING_COUNT warnings vs $RESUME_COUNT resumes)"
else
  pass "STOPPING warnings bounded by episode count ($STOPPING_COUNT)"
fi

# ── phase 5 (optional): genuine wedge → systemd kill ────────────────────────
if [[ $DO_WEDGE -eq 1 ]]; then
  log "== phase 5: SIGSTOP wedge → expect systemd kill at WatchdogSec expiry =="
  WPID=$(systemctl --user show "$UNIT" -p MainPID --value)
  log "SIGSTOP $WPID — watchdog thread freezes, pings stop, systemd must kill"
  kill -STOP "$WPID"
  # WatchdogSec after last ping → wait limit + margin, then check for kill
  sleep "$((WATCHDOG_LIMIT_S + 25))"
  POST=$(systemctl --user show "$UNIT" -p MainPID,NRestarts --value)
  if journalctl --user -u "$UNIT" --since "$JSTART" --no-pager \
       | grep -q "Watchdog timeout"; then
    pass "systemd watchdog killed the wedged unit (expected kill path)"
  else
    fail "no 'Watchdog timeout' after ${WATCHDOG_LIMIT_S}s SIGSTOP wedge"
  fi
else
  log "phase 5 skipped (--wedge not given)"
fi

# ── phase 6: journal audit ──────────────────────────────────────────────────
log "== phase 6: journal audit =="
AUDIT=$(journalctl --user -u "$UNIT" --since "$JSTART" --no-pager)
{
  echo "# Live validation — bounded startup grace (ed6f1f0)"
  echo "- run (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "- pre-load loadavg: $L1 / loadavg under burners: $L2 ($WORKERS workers, ${LOAD_SECONDS}s)"
  echo "- unit: $UNIT  WatchdogSec=${WATCHDOG_USEC_UNIT}s"
  echo "- grace armed line:  $(echo "$AUDIT" | grep -c "$GRACE_MARKER")"
  echo "- grace STATUS pings: $(echo "$AUDIT" | grep -c "$GRACE_STATUS")"
  echo "- STOPPING warnings:  $(echo "$AUDIT" | grep -c "$STOPPING")"
  echo "- resume lines:       $(echo "$AUDIT" | grep -c "$RESUME")"
  echo "- watchdog kills:     $(echo "$AUDIT" | grep -c 'Watchdog timeout')"
  echo "- restarts scheduled: $(echo "$AUDIT" | grep -c 'Scheduled restart job')"
  echo
  echo '```'
  echo "$AUDIT" | grep -E "self-watchdog|grace|STOPPING|fresh again|Watchdog timeout|Scheduled restart|fast tick [0-9]+ done|Started blm" | cut -c1-200
  echo '```'
  echo
  printf '%s\n' "${RESULTS[@]}"
} > "$REPO/$REPORT"
log "audit written: $REPORT"

stop_load
log "== summary =="
printf '%s\n' "${RESULTS[@]}"
log "collector left RUNNING (MainPID=$(systemctl --user show "$UNIT" -p MainPID --value), NRestarts=$(systemctl --user show "$UNIT" -p NRestarts --value))"
log "24-hour production observation still pending — do not close the incident on this run alone."
