# 12 — Production Runbook

Retrieval keywords: runbook, deploy, restart, rollback, systemd, blm-server,
blm-collector, healthz, logs, journalctl, backup, WAL, kill, read-only, no restart,
service topology, ports, activation, verify.

**What this pack answers:** how to inspect, change, deploy and roll back — safely,
and only when authorized.

---

## Service topology

| Thing | Value |
|---|---|
| repo | `/home/ubuntu/BLM` (branch `handoff-2026-09-07`; SSH remote `git@github.com:abwarren/BLM.git`) |
| units | `systemctl --user` → `blm-collector`, `blm-server` (live units under `~/.config/systemd/user/`) |
| API | `blm-server` on `127.0.0.1:2262` (drop-in binds 127.0.0.1) |
| collector | `python3 -m blm_v4.collector --tick 10` |
| pipeline DB | `blm_pokerbet.db` |
| clean/analytic DB | `blm_metrics_clean.db` |
| health | `/healthz` (unauth, 200) · `/api/v4/status` (AUTH-GATED → 401 without a session cookie — NOT "offline") |
| logs | `journalctl --user -u blm-collector` / `-u blm-server` |

**Runs from the WORKING TREE, not HEAD** — a dirty tree goes live.

---

## Rule 12.1 — The change sequence

```
BEFORE CHANGE
  ↓  inspect status   (git status; systemctl is-active; module mtimes)
  ↓  identify dirty files (owner boundaries — never touch another agent's work)
  ↓  run targeted tests (NOT the full suite on this host)
  ↓
APPLY CHANGE  (minimal, consistent with existing style)
  ↓  test (focused; mutation-proof for correctness-critical fixes)
  ↓  deploy  (ONLY with explicit authorization; record the exact command)
  ↓  health check  (/healthz 200; unit active; a fresh snapshot)
  ↓  production verification  (live API / DB / journal evidence)
```

**Source.** `AGENTS.md` §2.7; user rules.

**Forbidden.** Restart/deploy/commit/push without their own authorization; touching
the dirty tree; a restart to make a test pass.

---

## Rule 12.2 — Verification ladder (report each as explicit pass/fail)

1. `systemctl --user is-active blm-collector blm-server` → both `active`.
2. collector journal shows a recent tick with `errors=0`; `state/collector_state.json`
   `last_tick_at` advances.
3. newest `snapshots.captured_at` is within seconds (proves writes, not just uptime).
4. `/healthz` → 200; `/api/v4/status` → 401 (expected, auth-gated — NOT offline).
5. your probe's verdict == the real function's verdict.
6. nothing in the BLM tree was created/modified: `git -C /home/ubuntu/BLM status
   --short` unchanged vs baseline; `blm_betting.db` row counts unchanged. **Verify
   MTIMES before claiming you changed nothing** — a concurrent agent can edit
   `blm_v4/*` mid-session; report it as a separate finding.

**Source.** skill "Verification ladder".

**Forbidden.** Claiming health from uptime alone.

---

## Rule 12.3 — Activating a fix (restart) is its OWN authorization and needs proof

**Rule.** Restarting changes live state; it requires explicit authorization. After a
restart, record pre/post state and prove the RUNNING process has the fix.

**Source.** skill `references/activating-a-verified-fix.md`; Rule 0.8.

**Recommended steps.**

```
# pre-state
systemctl --user show blm-server -p MainPID -p ExecMainStartTimestamp -p NRestarts
stat -c '%y %n' blm_v4/<changed>.py
sha256sum blm_v4/<changed>.py
# restart ONLY the target unit (via a /tmp/*.sh file if the inline cmd is denied)
systemctl --user restart blm-server.service
# proof: behavioural counter unreachable under old code; module sha unchanged by activation
```

**Valid.** "`COUNT(*) attempt>=4` 0 → 6 after restart (unreachable pre-fix)."

**Invalid.** "I restarted and it works" with no counter/sha.

**Edge.** A watchful self-watchdog can restart the collector on its own; read the
journal before attributing a PID change to yourself.

**Forbidden.** Restarting both units "to be safe"; raising a timeout to hide a flap.

---

## Rule 12.4 — Which unit a fix lives in

**Rule.** Map the fix to the unit that runs the worker:

| Fix domain | Unit | Restart command |
|---|---|---|
| Collector / parser / capture | `blm-collector` | `systemctl --user restart blm-collector.service` |
| API / routers / result reconciler / settle worker / scorecard sweep | `blm-server` | `systemctl --user restart blm-server.service` |

**Source.** `server.py` (spawns `ResultReconcilerWorker`, `SettleWorker`, sweeps);
`blm_v4/collector.py` (the collector).

**Why.** A finals/retry fix lives in `blm-server` (the reconciler runs there); a
capture fix in `blm-collector`.

**Forbidden.** Restarting the wrong unit or both.

---

## Rule 12.5 — Database safety

**Rule.** Never modify historical data; no backfill/rewrite/delete of raw snapshots.
For a drain/repair, prefer the idempotent, provenance-guarded paths
(`_persist_result` upsert `WHERE final_result_status != 'OK'`; `_stamp_unresolved`
insert-only). A read-only COPY via SQLite `backup()` is the safe way to run an
end-to-end proof.

**Source.** `DECISIONS.md` §12.F; `blm_v4/wal_hygiene.py`.

**Forbidden.** `PRAGMA wal_checkpoint` against a live DB while the collector runs;
dropping/rebuilding a production DB; writing during READ-ONLY.

---

## Rule 12.6 — READ-ONLY means no restart

**Rule.** A READ-ONLY directive forbids every write AND every restart. Do not
restart to "verify" anything.

**Source.** the user's directives.

**Forbidden.** `systemctl --user restart ...` under a READ-ONLY directive.

**Verify.** handoff §11 records RESTARTED: NO.

---

## Rule 12.7 — Rollback

**Rule.** Roll back by checking out the previous code and restarting the affected
unit — but a rollback changes live state and needs its own authorization, and must
never rewrite history or disturb the dirty tree.

**Source.** `AGENTS.md` §2.1; `DECISIONS.md` §15.

**Forbidden.** `git reset --hard`, force-push, or discarding owner work to roll back.

**Verify.** handoff §11 records ROLLBACK PERFORMED: YES/NO with the command.

---

## Rule 12.8 — `deploy/*.service` are STALE

**Rule.** `deploy/blm-collector.service` / `deploy/blm-server.service` describe an
OLD deployment (user `gdi`, `/home/gdi/BLM`, `--tick 20`). The LIVE units are the
systemd `--user` units (user `ubuntu`, `/home/ubuntu/BLM`, `--tick 10`). Do not
trust or edit `deploy/*.service` as production.

**Source.** live `~/.config/systemd/user/blm-collector.service`;
`DECISIONS.md` §12.B is STALE. See CONTRADICTIONS.md.

**Verify.** `systemctl --user cat blm-collector blm-server`.
