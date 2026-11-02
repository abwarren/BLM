#!/usr/bin/env bash
# blm-wal-fold.sh — reclaim the BLM WAL at a quiet window (operator decision
# 2026-11-02, option 1: size-triggered, hourly, exits below threshold).
#
# WHY THIS EXISTS: PASSIVE folds frames but NEVER gives the file back, and
# TRUNCATE cannot get its exclusive lock while the collector commits every ~2 s
# (measured 2026-11-02: 15 s busy_timeout, checkpointed_frames=0, busy=1, file
# unchanged at 80 MB).  On this box the file is only reclaimable when the writer
# pauses for a few seconds — so this unit pauses it, truncates, and puts it back.
#
# COST: one short capture gap (typically 10-25 s).  The collector's boot
# backfill is paced (commit e65871b), so the restart itself does not stall.
#
# FAIL-SAFE: the collector is ALWAYS restarted — on success, on truncate
# failure, on interrupt, and on any error path — and the script then PROVES it
# by waiting for a fresh completed fast cycle before exiting 0.  A fold script
# that can leave capture down is worse than a big WAL.
set -uo pipefail

THRESHOLD_MB="${BLM_WAL_FOLD_THRESHOLD_MB:-192}"
DB="${BLM_WAL_FOLD_DB:-/home/ubuntu/BLM/blm_pokerbet.db}"
BUSY_MS="${BLM_WAL_FOLD_BUSY_MS:-6000}"
#: Level 1 is FREE or nothing: it either wins at once (measured 1 s) or it does
#: not win at all, and a long wait there only widens the window in which the
#: scorecard/settle writes can see `database is locked` (observed 2026-11-02
#: 02:24: scorecard_run_failed at result_policy.flag_conflict).
BUSY_MS_L1="${BLM_WAL_FOLD_BUSY_MS_L1:-2000}"
ATTEMPTS="${BLM_WAL_FOLD_ATTEMPTS:-3}"
#: Only above this size is a capture gap spent on the fold.  Below it the free
#: attempt is repeated every tick instead (the scans give gaps often enough).
FORCE_MB="${BLM_WAL_FOLD_FORCE_MB:-320}"
UNIT="blm-collector"
ROOT="${BLM_WAL_FOLD_ROOT:-/home/ubuntu/blm-fix-price-mapping}"
DRY=""
[ "${1:-}" = "--dry-run" ] && DRY=1

log() { printf 'wal-fold: %s\n' "$*"; }

size_mb() {
  if [ -f "$DB-wal" ]; then echo $(( $(stat -c %s "$DB-wal") / 1048576 )); else echo 0; fi
}

before="$(size_mb)"
log "wal=${before}MB threshold=${THRESHOLD_MB}MB db=$DB"
if [ "$before" -lt "$THRESHOLD_MB" ]; then
  log "below threshold — nothing to do"
  exit 0
fi
if [ -n "$DRY" ]; then
  log "dry run — would stop $UNIT, TRUNCATE, restart $UNIT"
  exit 0
fi

restarted=0
restore() {
  if [ "$restarted" -eq 0 ]; then
    log "restarting $UNIT"
    systemctl --user start "$UNIT" || log "ERROR: start $UNIT failed"
    restarted=1
  fi
}
# SIGTERM does not run an EXIT trap; the collector must come back either way.
trap restore TERM INT

# ── LEVEL 1: try WITHOUT taking anything down ────────────────────────────────
# Measured 2026-11-02: the blocker is a READER (the pace/stats scans run
# 282-579 s out of every 1800 s, holding a snapshot), not the writer — which is
# why a stop-the-collector window failed three times in a row while a scan was
# in flight (`busy=1 copied=198 held_back=21231`, identical each attempt) and
# why the very same pass once succeeded in 0.0 s with the collector RUNNING.
# So the cheap attempt is also the correct first one: it costs no capture at all
# and wins whenever the box happens to be between scans.
log "level 1 — single pass, nothing stopped (${BUSY_MS_L1}ms wait)"
/usr/bin/python3 - "$DB" "$BUSY_MS_L1" <<'PY'
import sys
sys.path.insert(0, "/home/ubuntu/blm-fix-price-mapping")
from blm_v4 import wal_hygiene as W
db, busy = sys.argv[1], int(sys.argv[2])
r = W.passive_checkpoint_once(db, threshold_mb=0.0001, hard_cap_mb=0.0001,
                              busy_timeout_ms=busy)
print("wal-fold: mode=%s busy=%s log=%s copied=%s held_back=%s wal=%d"
      % (r.get("mode"), r.get("busy"), r.get("log"),
         r.get("checkpointed_frames"), r.get("held_back"),
         r.get("wal_bytes") or 0))
sys.exit(0 if r.get("busy") == 0 else 3)
PY
l1=$?
trap - TERM INT
if [ "$l1" -eq 0 ]; then
  log "OK level 1 truncated the file with nothing stopped · wal $(size_mb)MB"
  exit 0
fi

# ── LEVEL 2: only for a WAL that is genuinely large — stop, retry, restart ───
if [ "$(size_mb)" -lt "$FORCE_MB" ]; then
  log "level 1 lost the lock; wal $(size_mb)MB < force ${FORCE_MB}MB — leaving it, next tick retries"
  exit 0
fi
log "level 2 — wal $(size_mb)MB >= ${FORCE_MB}MB, stopping $UNIT for the fold"
trap restore TERM INT

if ! systemctl --user stop "$UNIT"; then
  log "ERROR: could not stop $UNIT — aborting without touching the WAL"
  exit 1
fi
sleep 2                     # let the last transaction commit and close

log "fold starting (up to ${ATTEMPTS} attempts, ${BUSY_MS}ms each)"
# The collector is the continuous writer, but the SERVER's own workers (settle,
# reconciler, projections) keep committing too, so one attempt can still lose the
# exclusive lock (measured 2026-11-02: busy=1, copied=198 of 11,604).  Retry a
# few times inside the stopped window, then give up and restart — a longer
# outage buys nothing: what the truncate needs is a GAP, not a quiet minute.
rc=1
attempt=1
while [ "$attempt" -le "$ATTEMPTS" ]; do
  log "attempt $attempt/$ATTEMPTS"
  /usr/bin/python3 - "$DB" "$BUSY_MS" <<'PY'
import sys
sys.path.insert(0, "/home/ubuntu/blm-fix-price-mapping")
from blm_v4 import wal_hygiene as W
db, busy = sys.argv[1], int(sys.argv[2])
r = W.passive_checkpoint_once(db, threshold_mb=0.0001, hard_cap_mb=0.0001,
                              busy_timeout_ms=busy)
print("wal-fold: mode=%s busy=%s log=%s copied=%s held_back=%s wal=%d"
      % (r.get("mode"), r.get("busy"), r.get("log"),
         r.get("checkpointed_frames"), r.get("held_back"),
         r.get("wal_bytes") or 0))
sys.exit(0 if r.get("busy") == 0 else 3)
PY
  rc=$?
  [ "$rc" -eq 0 ] && break
  attempt=$((attempt + 1))
  if [ "$attempt" -le "$ATTEMPTS" ]; then sleep 3; fi
done
restore
trap - TERM INT

if [ "$rc" -ne 0 ]; then
  log "WARN: truncate did not win the lock in $ATTEMPTS attempts — file left as it was"
fi

# prove the collector is actually collecting again (a fresh COMPLETED cycle)
for _ in $(seq 1 18); do
  sleep 5
  # NOTE: never `grep -q` inside a pipeline under `pipefail` — grep exits at the
  # first match, journalctl takes SIGPIPE, and the pipeline reports FAILURE even
  # though the match was found (measured 2026-11-02: the fold worked, the proof
  # said it had not).
  ticks=0
  if systemctl --user is-active --quiet "$UNIT"; then
    ticks="$(journalctl --user -u "$UNIT" --since "-90 s" --no-pager 2>/dev/null \
             | grep -c 'fast tick .* done')"
  fi
  if [ "${ticks:-0}" -gt 0 ]; then
    after="$(size_mb)"
    log "OK wal ${before}MB -> ${after}MB · $UNIT active and ticking"
    exit 0
  fi
done
log "ERROR: $UNIT did not resume a completed fast cycle within 90 s — CHECK THE UNIT"
exit 1
