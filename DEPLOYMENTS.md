# BLM — DEPLOYMENT IDENTITY (read this before touching anything)

There are **TWO** deployments of the BLM product. They run the same code but are
NOT interchangeable. Confirm which box you are on before any command:

```bash
hostname     # portable-ubuntu = PRODUCTION   |   blm-stage-cpt = STAGE (Cape Town)
```

| | PRODUCTION | STAGE |
|---|---|---|
| hostname | `portable-ubuntu` | `blm-stage-cpt` (was `ip-172-31-1-213`, renamed 2026-10-26) |
| location | operator's data box, LAN `192.168.1.100` | AWS EC2 `i-0d6c4d01114f52bf4`, `13.247.58.81`, af-south-1, t3.large |
| role | **authoritative capture + alerts + Auto-Bet** | UI/API staging only — **never** authoritative |
| code tree(s) | `/home/ubuntu/BLM` (collector, git `handoff-2026-09-07`) + `/home/ubuntu/blm-fix-price-mapping` (server, git `fix/price-mapping-2026-10-06`) | `/home/ubuntu/blm` — **rsync mirror, NOT a git repo** |
| git | yes, dirty working trees are the deploy model | none (copy) |
| services | user units `blm-collector` + `blm-server` | user unit `blm-server` only |
| collector | `python3 -m blm_v4.collector --tick 10`, WD `/home/ubuntu/BLM` | **DISABLED** (`BLM_DISABLE_COLLECTOR=1`) |
| data root | `/home/ubuntu/BLM` — `blm_pokerbet.db` **63 GB**, `blm_metrics_clean.db` **5.3 GB** | `/srv/blm/data` — `blm_pokerbet.db` **418 MB**, **no** `blm_metrics_clean.db` |
| data freshness | live (capturing now) | **frozen at 2026-10-23T20:34Z** — historical fixture only |
| API | `127.0.0.1:2262` (HOST=0.0.0.0 per unit) | `127.0.0.1:2262` behind nginx `:80` (`/stage-health` → `blm-stage ok`) |
| Auto-Bet | **ARMED** at unit level (`90-live-submit.conf`: `BETTING_DRY_RUN=false`, `BETTING_BROWSER_SUBMIT=true`, limits 1000/200) | **DISARMED** — no betting env vars, fails closed |
| auth | gate on both (`/healthz` → `{"status":"ok","service":"blm","auth":true,...}`) | same |
| other products on box | Roulette448 (`:4480`), NEW_E-R (`:8090`) | E&R poker (`/home/ubuntu/er`, `:8090`) — **different product, ignore for BLM** |

## Hard rules

1. **Never** run a collector on stage. A second capture pipeline would double-scrape
   PokerBet, create a competing alert state, and can never be reconciled with prod.
2. **Never** set `BETTING_DRY_RUN=false` / `BETTING_BROWSER_SUBMIT=true` on stage, and
   never grant stage the prod betting-limits drop-ins. Stage Auto-Bet must fail closed.
3. **Never** copy stage DBs over prod, and never treat a stage alert/row as evidence
   about production performance — stage data stopped 2026-10-23T20:34Z.
4. Prod's authoritative numbers come from prod only: `/home/ubuntu/BLM/blm_pokerbet.db`
   + `blm_metrics_clean.db`.

## Syncing stage FROM production (the only supported direction)

Reference tree for the stage is the **server** tree, not the collector tree:

```bash
cd /home/ubuntu/blm-fix-price-mapping
rsync -az --checksum --delete \
  --exclude='.git' --exclude='.venv/' --exclude='__pycache__/' \
  --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' \
  --exclude='backups/' --exclude='logs/' --exclude='*.log' \
  --exclude='STAGE_ADMIN.txt' \
  ./ ubuntu@13.247.58.81:/home/ubuntu/blm/
ssh ubuntu@13.247.58.81 'systemctl --user restart blm-server && curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:2262/healthz'
```

**Do NOT** sync `/home/ubuntu/BLM` (collector tree) onto stage: it lacks the server-only
modules that the stage server imports — `blm_v4/trade_window.py`, `blm_v4/result_verifier.py`,
`blm_v4/betting/position.py`, `blm_v4/betting/measurement.py`, `blm_v4/execution/browser_bridge.py`,
`blm_v4/assistant/` — and `--delete` would remove them and break the stage server.

Verified 2026-10-26: stage tree byte-identical to `/home/ubuntu/blm-fix-price-mapping`
(0 content diffs), stage server restarted on the synced code, `/healthz` 200, dashboard 200.
The sync provenance stamp lives OUTSIDE the synced tree on stage, at
`/home/ubuntu/BLM_STAGE_PROVENANCE.txt` (source branch/HEAD/dirty count + file hash +
the parity claim) — check it before believing stage runs current code.

## Stage ops contract (installed 2026-10-26)

**Disk** — the EC2 root stays at 29 GB; data lives on its own partition:

| | |
|---|---|
| volume | `vol-07e854ea322adb1a5`, gp3, **30 → 150 GB** (modified 2026-10-26) |
| partition | `nvme0n1p17` = **118 GB** ext4, label `blmdata` |
| mount | `/srv/blm/data` (fstab `defaults,nofail,noatime`, kernel/fstab agree) |
| root fs | untouched at 29 GB — the Ubuntu AMI puts p14/p15/p16 after p1, so p1 **cannot** grow; a new partition is the only in-place option |

**Maintenance timers (systemd --user on stage, linger enabled)**

| unit | schedule | what it does |
|---|---|---|
| `blm-health.timer` | every 2 min | `/healthz` 200 within 5 s, 3 strikes → `restart blm-server`; data-root mount check; disk ≥85% warn / ≥92% triggers retention; collector-staleness restart *only if* a collector unit is installed |
| `blm-retain.timer` | daily 03:30 UTC | rows older than **30 days** → NDJSON.gz to `s3://…/archive/<table>/dt=…`, verify size+sha256+rows via head-object, **then** delete; weekly VACUUM when freelist >10% |
| `blm-dbcheck.timer` | Sun 04:15 UTC | `PRAGMA quick_check` on every DB + sizes → `s3://…/reports/` |
| `blm-overflow` (cron.d, existing) | every 15 min | artifact sync to `s3://…/data`, daily `.backup` DB images, prune local copies only after S3 confirms |

**Restart posture** — `blm-server`: `Restart=always`, `RestartSec=5`, `StartLimitIntervalSec=0`, `MemoryHigh 1500M`/`MemoryMax 2G`; watchdog restarts it after 3 failed probes (measured: stop → 3 probes → restart → 200 in ~9 s). `nginx`: `Restart=always`, `RestartSec=2`. journald capped at 1 GB / 30 days; `/var/log/blm/*.log` + `/home/ubuntu/logs/*.log` rotate weekly ×8. `unattended-upgrades` enabled.

**S3 layout / lifecycle** on `blm-overflow-243047261878-afsouth1`:
`archive/` → STANDARD_IA @30 d, GLACIER_IR @90 d, expire @730 d · `snapshots/` expire @90 d · `reports/` · `data/`.

## Data mirror — capacity is now sufficient (mirror still paused)

After the 2026-10-26 resize the data volume has **118 GB** (116 GB free) — enough for the
current prod DB (59 GiB) plus a 30-day hot window with VACUUM headroom. The prod→stage
mirror itself is still **PAUSED**: the cron line in prod's crontab is commented
`# PAUSED-MIRROR: */5 * * * * /home/ubuntu/bin/blm-mirror-sync.sh`. Resuming is a
one-line uncomment on PROD (it is read-only against prod and delta-based via
`/home/ubuntu/.blm-stage-sync.json`, watermarks currently at 2026-10-23T20:34Z), and it
adds load to the prod box — operator decision.

Stage data today is a 31-hour slice (2026-10-22T13:42 → 2026-10-23T20:34Z), which is why
retention finds almost nothing to archive yet.

Prod itself is at 59 GiB of a 233 GB disk (65 GB free, 71% used) — pruning prod's own
>30-day rows would cut the historical window (the TC-X cohort window starts 2026-09-05),
so that is a separate operator decision, not implied by this document.
