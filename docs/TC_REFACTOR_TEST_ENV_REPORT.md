# TC cohort refactor — EC2 test environment identity & isolation report

**Status:** Phase 0 deliverable (strategy doc §2.2 / §9). Recorded 2026-10-26.
**Scope:** proves WHERE the refactor was implemented and that it cannot reach production.
No secrets are reproduced here.

## 1. Identity

| item | value |
|---|---|
| hostname | `blm-stage-cpt` |
| AWS instance | `i-0d6c4d01114f52bf4`, `13.247.58.81`, af-south-1, t3.large |
| AWS account / role | `243047261878`, instance profile `blm-stage-s3-profile` → role `blm-stage-s3-role` (inline policy `blm-overflow-access`, S3 only) |
| OS / kernel | Ubuntu 24.04.5 LTS, `7.0.0-1014-aws` |
| login user | `ubuntu` (passwordless sudo; no other interactive users) |
| repo (clone) | `https://github.com/abwarren/BLM.git`, branch `feature/tc-cohort-refactor-test`, base `docs/environment-boundaries-test-first` @ `5988e19` |
| working tree | `/home/ubuntu/blm-tc-refactor` (separate from the deployed `/home/ubuntu/blm`) |

**Production** is a *different* host (`portable-ubuntu`, LAN `192.168.1.100`) serving
`https://blm.haaats.xyz/`. This report covers only the EC2 test host.

## 2. Services / listeners on the test host

| unit | state | notes |
|---|---|---|
| `blm-server.service` (user) | active | the deployed staging API on `127.0.0.1:2262`, behind nginx `:80`; **collector disabled** (`BLM_DISABLE_COLLECTOR=1`) |
| `blm-health.timer` | active | 2-minute liveness watchdog (repo: restart after 3 failed probes) |
| `blm-retain.timer` | active | 30-day S3 overflow retention |
| `blm-dbcheck.timer` | active | weekly `PRAGMA quick_check` |
| `nginx` | active | reverse proxy `:80` → 2262 |
| `er-*` | active | **different product** (E&R poker, :8090) — out of scope |

No collector process runs on this host (`pgrep -af collector` → none).

## 3. Data paths (all local, no remote targets)

| path | device | purpose |
|---|---|---|
| `/srv/blm/data/*.db` | `/dev/nvme0n1p17` (118 GB ext4, label `blmdata`) | test data root (`BLM_DATA_ROOT`) |
| `/srv/blm/data/blm_pokerbet.db` | same | 272 MB, 1,114 games, 163,500 snapshots (slice 2026-10-22T13:42 → 10-23T20:34Z) |
| `/srv/blm/data/derived/cohort_rescore*.db` | same | derived read-model written by this refactor |
| `/home/ubuntu/blm-tc-refactor` | `/` (29 GB, root fs) | code only, no data |

There is **no `blm_metrics_clean.db`** on this host: the collector's clean pipeline never
ran here, so the rescore reconstructs firing-time inputs from `snapshots` + `games` +
`game_results` (documented in `blm_v4/cohorts/rescore.py`).

## 4. Isolation evidence (what was actually executed)

1. **Production is not addressable from here.**
   `nc -z -w2 192.168.1.100 22` → unreachable; `nc -z -w2 192.168.1.100 2262` → unreachable.
   There is no VPN, tunnel or route to the production LAN.
2. **No outbound credentials.** `~/.ssh/` contains only `authorized_keys` — no private
   keys, so this host cannot SSH anywhere. The AWS role is scoped to S3 (`blm-overflow-access`).
3. **No production secrets in the environment.** `env | cut -d= -f1` shows no `AWS_*`,
   no token/secret/password names, and no BLM production variables.
4. **The only cross-environment data flow is inbound and paused.** Production pushes a
   delta mirror *to* this host (`blm-mirror-sync.sh`, cron line currently commented out);
   this host has no path to write to production.
5. **Read-only behaviour is measured, not asserted.** Re-running the rescore leaves the
   source DB byte-identical:
   `stat` before `272490496 1793048878` → after `272490496 1793048878`.
   All API handlers open the derived DB with `mode=ro` (test pins file size+mtime).
6. **Auto-Bet is disarmed on this host.** No `BETTING_*` variables are set in the unit,
   so the executor fails closed; nothing in this refactor writes betting configuration,
   and the dashboard panel is display-only.
7. **Plain HTTP.** The designated entry point is `http://13.247.58.81/`; no production
   credential was transmitted through it. The UI evidence run used the stage-local
   login only.

## 5. Where the refactor actually runs

The deployed staging service (`:2262` behind `:80`) keeps serving the **mirror of the
production server tree** and was **not** replaced: the branch under test is based on
`docs/environment-boundaries-test-first` (`5988e19`), which predates the live server
branch (`fix/price-mapping-2026-10-06`). Deploying it over the mirror would regress
stage and destroy the parity evidence from 2026-10-26.

Acceptance therefore runs the branch as a **parallel instance**:
`PORT=2263`, `BLM_DATA_ROOT=/srv/blm/data`, `BLM_COHORT_DB=/srv/blm/data/derived/cohort_rescore.db`,
collector disabled. Promoting the branch into the designated entry point requires a
rebase onto the live server branch first — a separate step, not performed here.

## 6. What was NOT done (production boundary)

- No production file edit, deploy, restart, migration, backfill or rescore write.
- No change to any production DB, service, credential, proxy or DNS.
- No Auto-Bet change (enabled state, cohort, stake, caps, kill switch, submission).
- No production data copied into this environment or into git.
