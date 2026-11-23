# AUTOBET GATE DIRECTIVE — STRENGTH × FINGERPRINT FILTER
**Issued:** 2026-11-15  
**Authority:** abwarren (owner)  
**Status:** ACTIVE — implement against existing auto-bet pipeline  
**Supersedes:** None (additive gate on top of existing 75% alert trigger)

---

## 1. OBJECTIVE

Add a pre-execution qualification gate to the auto-bet pipeline that only allows
bets to proceed when the triggering alert meets a minimum strength + fingerprint
threshold. All existing safety rules, stake logic, and execution contracts remain
unchanged. This directive modifies the QUALIFICATION criteria only — not execution,
not stake sizing, not rung logic.

---

## 2. EVIDENCE BASIS

30-day backtest (2026-10-16 → 2026-11-15), 75–92% progress band, shortfall ≥ 0,
pace > 1.04x league average, **excluding cyber-basketball-2k26-matches**.

| Combo                    | n   | Win%  | Flat units | Per bet  |
|--------------------------|-----|-------|------------|----------|
| 1.20–1.35 + any FP       | 21  | 100%  | +16.17u    | +0.770u  |
| 1.20–1.35 (all, no Q3)   | 59  | 93.2% | +38.35u    | +0.650u  |
| **1.20–1.35 ALL**        | 80  | 95.0% | +54.52u    | +0.682u  |
| ≥1.35 + any FP           | 15  | 93.3% | +9.78u     | +0.652u  |
| **≥1.35 ALL**            | 42  | 90.5% | +25.26u    | +0.601u  |
| betual-nba (all)         | 92  | 91.3% | +56.68u    | +0.616u  |
| 1.10–1.20 + C5 or C3     | 32  | 84.4% | +15.79u    | +0.493u  |
| 1.10–1.20 + R2           | 30  | 83.3% | +14.25u    | +0.475u  |
| **1.10–1.20 + any FP**   | 32  | 84.4% | +15.79u    | +0.493u  |
| 1.10–1.20 ALL            | 201 | 77.6% | +75.12u    | +0.374u  |
| < 1.10 + any FP          | 50  | 70.0% | +11.95u    | +0.239u  |
| < 1.10 ALL               | 304 | 71.4% | +80.09u    | +0.264u  |

**Margin analysis (top tier — ≥1.10 + any FP):**
- Average WIN margin: +14.8 pts under the line
- Average LOSS overage: +5.0 pts over the line
- 4 of 6 losses were within 5 pts of the line (marginal, not blowouts)

**Key finding:** When Q3 data is available and NO fingerprint fires, edge drops to
near zero or negative. The gate below is designed to capture this.

---

## 3. THE GATE — REQUIRED CONDITIONS FOR AUTO-BET QUALIFICATION

An alert MAY proceed to execution **only if ALL of the following are true**:

### 3.1 — Competition exclusion (hard block)
```
competition_slug != "cyber-basketball-2k26-matches"
```
Cyber 2K26 is permanently excluded from auto-bet. It runs 24/7 at a volume that
dilutes every signal tier. All other Betual competitions are permitted.

### 3.2 — Progress band (existing, unchanged)
```
75.0 <= progress_pct <= 92.0
```
No change from current production gate.

### 3.3 — Shortfall present (existing, unchanged)
```
required_pts_per_min > actual_pts_per_min   (shortfall >= 0)
```

### 3.4 — Minimum strength band
```
req_ratio = required_pts_per_min / competition_avg_pace
req_ratio > 1.04   (existing production minimum)
```
**NEW:** Auto-bet TIERS based on req_ratio (determines bet eligibility and stake
priority — see §4):

| Tier | req_ratio range | Backtest win% | Action         |
|------|----------------|---------------|----------------|
| A    | >= 1.20        | 90.5–95.0%    | ALWAYS qualify |
| B    | 1.10–1.20      | 77.6–84.4%    | Qualify IF FP  |
| C    | 1.04–1.10      | 71.4%         | Qualify IF FP  |
| –    | <= 1.04        | below edge    | NEVER qualify  |

### 3.5 — Fingerprint gate (NEW — applies to Tiers B and C only)

**Tier A (req_ratio >= 1.20):** No fingerprint required. Bet unconditionally
(95% win rate holds with or without Q3 data in 30-day backtest).

**Tier B (1.10–1.20) and Tier C (1.04–1.10):** At least ONE of the following
fingerprints must be TRUE (not UNAVAILABLE):

| Code | Condition                                               |
|------|---------------------------------------------------------|
| C3   | req_ratio > 1.04 AND q3_ratio < 1.00                   |
| C5   | req_ratio > 1.10 AND q3_ratio < 1.00                   |
| R2   | q3_ratio < 0.90x league Q3 average                     |

Where:
```
q3_ratio = (q3_home_score + q3_away_score) / q3_minutes / competition_avg_q3_pace
```
Source authority: `blm_v4/live_analytics/under_fingerprints.py` —
`evaluate_fingerprints()`. Use this function directly; do not reimplement.

**If Q3 data is unavailable (fingerprints = UNAVAILABLE):**
- Tier A: proceed (Q3 not required at this strength)
- Tier B: proceed (77.6% base rate without FP confirmation is acceptable)
- Tier C: DO NOT bet (edge is unconfirmed below 1.10x without Q3)

---

## 4. TIER PRIORITY AND STAKE SIZING

Existing BLM Unit Size and rung logic remain unchanged. The tier determines
**ladder starting position** when the system supports priority injection
(implement only if the existing rung/stake API supports it without modification
to the safety contract):

| Tier | Ladder behaviour         |
|------|--------------------------|
| A    | Standard rung (no change)|
| B    | Standard rung (no change)|
| C    | Standard rung (no change)|

**Do not modify stake sizing or rung reset logic.** This directive changes
qualification only. The ladder continues to run as currently wired.

---

## 5. IMPLEMENTATION CONTRACT

### 5.1 — Where to implement
Add the gate in the **alert qualification step** that feeds the existing auto-bet
worker, before `executor.evaluate(..., claim=True)` is called.

Likely location: `blm_v4/betting/worker.py` or wherever the alert is consumed
before the execution chain begins. Read the current worker before writing.

### 5.2 — Gate function signature (suggested)
```python
def qualifies_for_autobet(
    competition_slug: str,
    progress_pct: float,
    required_pts_per_min: float,
    actual_pts_per_min: float,
    competition_avg_pace: float,
    competition_avg_q3_pace: float | None,
    q3_total_points: int | None,
    q3_minutes: float | None,
) -> tuple[bool, str]:
    """
    Returns (True, tier) if the alert qualifies, (False, reason) if not.
    Pure function — no DB writes, no side effects.
    """
```

### 5.3 — Fail-closed rule
Any missing or non-finite value in the gate inputs → `(False, "MISSING_INPUT")`.
Never fabricate a value. Consistent with Rule 0.4.

### 5.4 — Logging
On every evaluation log: `gid`, `slug`, `req_ratio`, `q3_ratio`, `tier`,
`fingerprints_fired`, `qualified` (True/False), `reason`. This feeds the
audit trail.

### 5.5 — Tests required before activation
Write tests covering:
- Tier A qualifies without FP
- Tier A qualifies when FP fires
- Tier B qualifies when C5 fires
- Tier B qualifies when R2 fires
- Tier B does NOT qualify when Q3 available and no FP fires
- Tier C with Q3 unavailable → does not qualify
- Cyber-2k26 → always blocked
- Missing avg_pace input → fail-closed
- req_ratio exactly 1.20 → Tier A
- req_ratio exactly 1.10 → Tier B

---

## 6. DO NOT

- Do NOT modify `under_alert.py` trigger logic or the 75% checkpoint
- Do NOT modify stake sizing, `stake.py`, or `rung.py`
- Do NOT modify the AUTOBET_SAFETY_CONTRACT
- Do NOT activate on production without passing the tests in §5.5
- Do NOT deploy without explicit restart authorization from abwarren
- Do NOT use the cyber league as a test case for the gate (always blocked)
- Do NOT recompute `evaluate_fingerprints()` — import and call it directly
- Do NOT mix classification (`CYBER_2K26`, `BETUAL_NBA`) in place of `competition_slug`

---

## 7. SUCCESS CRITERIA

1. Gate function exists as a pure function with the signature in §5.2
2. All tests in §5.5 pass
3. Gate is wired into the worker before `claim=True`
4. No existing tests broken
5. Audit log emits `qualified`/`reason` on every evaluation
6. `blm_betting.db` unchanged (read-only during implementation)
7. No production restart until explicitly authorized

---

## 8. EVIDENCE LOCATION

Backtest script and results available at:
```
/home/ubuntu/BLM/scripts/analysis_gate_volume_sweep_2026-11-11.py  (reference)
```
30-day backtest was run read-only against:
```
/home/ubuntu/BLM/blm_pokerbet.db
/home/ubuntu/BLM/blm_metrics_clean.db
```
Fingerprint logic authority:
```
/home/ubuntu/BLM/blm_v4/live_analytics/under_fingerprints.py
/home/ubuntu/BLM/blm_v4/live_analytics/fingerprint_c5.py
/home/ubuntu/BLM/blm_v4/live_analytics/competition_pace.py
```

---

## 9. VERDICT

**READY FOR IMPLEMENTATION — NOT YET ACTIVATED**

Implementation and testing may proceed. Production activation requires a separate
explicit authorization from abwarren including a restart instruction.
