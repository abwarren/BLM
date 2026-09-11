"""Forensic relative-pace audit — regression freeze of the key numbers.

Re-runs the INDEPENDENT forensic audit engine
(``scripts/forensic_relative_pace_audit_2026-09-11.py``) against the CURRENT
production archive and freezes its headline results:

  * primary condition (actual < own league/state prior mean AND required >=
    own prior mean): 69.75% observation-UNDER  (frozen 2026-09-11 archive)
  * equal-game UNDER rate of the primary cell: 73.02%
  * leakage: 0 self / 0 future / 0 same-game members in the sampled
    benchmark member scans
  * structure: <2.5 minutes fully excluded, 2.50 included, every benchmark
    N >= 30 (MIN_BENCHMARK_N)

The freeze tolerances are deliberately tight: the exact rates drift by a few
hundredths of a point as the archive grows (older eras keep their settled
outcomes while new games arrive — see the audit's vintage table), so the
primary rates are frozen within +/-1.0pp of the audited values and the
support volumes must stay above 10,000 observations / 900 games.  Leakage
counts are frozen at exactly 0/0/0 — any nonzero value is a hard failure.

The audit engine opens every database READ-ONLY (uri mode=ro) and only ever
writes its own report path, which this test redirects into its tmp dir;
running the suite never mutates production data.  If the production archive
is unavailable the suite skips (frozen numbers cannot be re-verified without
the data).
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATH = REPO_ROOT / "scripts" / "forensic_relative_pace_audit_2026-09-11.py"
CLEAN_DB = Path("/home/ubuntu/BLM/blm_metrics_clean.db")
ANALYTICS_DB = Path(str(CLEAN_DB) + ".live_analytics.db")
PROD_DB = Path("/home/ubuntu/BLM/blm_pokerbet.db")

# ── frozen values (independent forensic audit, 2026-09-11 archive) ──────
PRIMARY_UNDER_PCT_FROZEN = 69.75      # observation-weighted, cell B
PRIMARY_EQUAL_GAME_UNDER_FROZEN = 73.02
PRIMARY_TOLERANCE_PP = 1.0            # archive growth moves the rate slightly
MIN_PRIMARY_OBS = 10_000              # support must not collapse
MIN_PRIMARY_GAMES = 900
MIN_ALL_BASELINE_PCT = 45.0           # all-observation UNDER% sanity floor
MAX_ALL_BASELINE_PCT = 55.0
CHECKPOINT_MIN_UNDER_PCT = 55.0       # every 1-minute band must clear this
MAX_VARIANT_DELTA_PP = 0.5            # construction-variant invariance band

pytestmark = pytest.mark.skipif(
    not (CLEAN_DB.exists() and ANALYTICS_DB.exists() and PROD_DB.exists()
         and ENGINE_PATH.exists()),
    reason="production archive or audit engine not available on this host",
)


def _load_engine():
    spec = importlib.util.spec_from_file_location(
        "forensic_relative_pace_audit", ENGINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _dbs_available() -> bool:
    try:
        con = sqlite3.connect(f"file:{CLEAN_DB}?mode=ro", uri=True, timeout=5)
        con.execute("SELECT 1")
        con.close()
        return True
    except sqlite3.Error:
        return False


@pytest.fixture(scope="module")
def audit(tmp_path_factory):
    """Run the full independent audit pipeline once and return its objects.

    The engine's report buffer is captured (and stdout silenced) so the test
    neither rewrites the canonical report file nor spams the log; the report
    text is returned for optional grep-style assertions.
    """
    if not _dbs_available():
        pytest.skip("production archive not readable")
    mod = _load_engine()
    tmp_out = tmp_path_factory.mktemp("forensic_audit") / "audit_report.txt"
    orig_out, orig_out_fn, orig_report = mod.OUT, mod.out, list(mod.REPORT)
    mod.REPORT.clear()
    mod.out = lambda s="": mod.REPORT.append(s)   # silence stdout, keep buffer
    try:
        mod.OUT = str(tmp_out)                    # never the canonical report
        ctx = mod.audit_a()
        rows, _excl = mod.build_population(ctx["ledger"], ctx["results"])
        idx_by_key = mod.build_benchmarks(rows)
        cd = mod.audit_c_d(rows)
        esum = mod.audit_e(cd["mature"])
        bands = mod.audit_f(rows)
        b2 = mod.audit_b2(cd["mature"], idx_by_key)
        hres = mod.audit_h(rows, idx_by_key)
        ires = mod.audit_i(ctx["ledger"])
    finally:
        mod.OUT, mod.out = orig_out, orig_out_fn
        mod.REPORT.clear()
        mod.REPORT.extend(orig_report)
    return {"mod": mod, "rows": rows, "cd": cd, "esum": esum,
            "bands": bands, "b2": b2, "hres": hres, "ires": ires}


# ── AUDIT A/B — population structure ────────────────────────────────────

def test_eligibility_gate_2_5_is_inclusive_and_absolute(audit):
    rows = audit["rows"]
    assert rows, "no eligible observations reconstructed"
    assert min(r.rem for r in rows) >= 2.5, \
        "remaining_game_minutes < 2.5 leaked into the eligible population"
    assert any(abs(r.rem - 2.5) < 1e-9 for r in rows), \
        "2.50 must be INCLUDED in the eligible population"


def test_every_mature_benchmark_meets_min_n(audit):
    mod = audit["mod"]
    mature = [r for r in audit["rows"] if r.bench["mean"] is not None]
    assert mature
    assert all(r.bench["n"] >= mod.MIN_N for r in mature), \
        "N>=30 maturity gate violated: an immature population produced a mean"


def test_pushes_never_enter_under_or_over_rates(audit):
    """Freeze tripwire: the 2026-09-11 archive contains zero pushes
    (final_total == live line).  If one appears, the served-rate semantics
    (decisive vs push-inclusive) must be re-examined before unfreezing."""
    pushed = [r for r in audit["rows"] if r.push]
    assert not pushed, \
        f"{len(pushed)} push observations appeared; UNDER/OVER rate " \
        "semantics must be re-examined before unfreezing"


# ── AUDIT C/D — the frozen headline ─────────────────────────────────────

def test_primary_condition_under_rate_frozen(audit):
    B = audit["cd"]["B"]
    assert B["n"] >= MIN_PRIMARY_OBS, \
        f"primary support collapsed to {B['n']} observations"
    assert B["games"] >= MIN_PRIMARY_GAMES, \
        f"primary game support collapsed to {B['games']}"
    assert B["under_pct"] == pytest.approx(PRIMARY_UNDER_PCT_FROZEN,
                                           abs=PRIMARY_TOLERANCE_PP), \
        f"primary observation-UNDER% {B['under_pct']:.2f} drifted from frozen " \
        f"{PRIMARY_UNDER_PCT_FROZEN}"
    assert B["eg"] == pytest.approx(PRIMARY_EQUAL_GAME_UNDER_FROZEN,
                                    abs=PRIMARY_TOLERANCE_PP), \
        f"primary equal-game UNDER% {B['eg']:.2f} drifted from frozen " \
        f"{PRIMARY_EQUAL_GAME_UNDER_FROZEN}"


def test_primary_condition_beats_every_other_cell(audit):
    cells = audit["cd"]["cells"]
    B = cells["B"]["under_pct"]
    assert B is not None
    for k in ("A", "C", "D"):
        assert cells[k]["under_pct"] is not None
        assert B > cells[k]["under_pct"], \
            f"cell B ({B:.2f}%) must under-rate cell {k} " \
            f"({cells[k]['under_pct']:.2f}%)"


def test_baseline_under_rate_is_coin_flip_like(audit):
    mature = audit["cd"]["mature"]
    dec = [r for r in mature if not r.push]
    base = 100.0 * sum(1 for r in dec if r.under == 1) / len(dec)
    assert MIN_ALL_BASELINE_PCT <= base <= MAX_ALL_BASELINE_PCT, \
        f"all-observation baseline {base:.2f}% left the coin-flip band"


def test_primary_direction_holds_in_every_competition(audit):
    esum = audit["esum"]
    assert esum, "no per-competition cells computed"
    for comp, d in esum.items():
        b = d.get("B(primary)")
        assert b is not None and b["n"] > 0, f"{comp}: empty primary cell"
        cc, dd, aa = d.get("C"), d.get("D"), d.get("A")
        assert b["under_pct"] > cc["under_pct"], \
            f"{comp}: cell B must under-rate cell C"
        assert b["under_pct"] > dd["under_pct"], \
            f"{comp}: cell B must under-rate cell D"
        assert b["eg"] > aa["eg"], \
            f"{comp}: equal-game UNDER% of B must beat cell A"


# ── AUDIT F — checkpoints ───────────────────────────────────────────────

def test_primary_condition_survives_all_checkpoints(audit):
    tbl1 = [(name, n, g, up, eg) for (name, n, g, up, eg) in audit["bands"]
            if up is not None]
    names = [b[0] for b in tbl1]
    for expected in ("[6.0,7.0)", "[5.0,6.0)", "[4.0,5.0)", "[3.0,4.0)",
                     "[2.5,3.0)"):
        assert expected in names, f"checkpoint {expected} missing"
    for (name, n, g, up, eg) in tbl1:
        assert n > 0 and g > 0, f"{name}: empty checkpoint"
        assert up > CHECKPOINT_MIN_UNDER_PCT, \
            f"{name}: primary UNDER% {up:.2f} fell below the frozen band"
        assert eg > CHECKPOINT_MIN_UNDER_PCT, \
            f"{name}: equal-game UNDER% {eg:.2f} fell below the frozen band"


# ── AUDIT H — leakage (hard 0/0/0 freeze) ───────────────────────────────

def test_leakage_zero_self_future_and_same_game(audit):
    h = audit["hres"]
    assert h["bad_self"] == 0, "a benchmark contained the CURRENT observation"
    assert h["bad_future"] == 0, "a benchmark contained captured_at >= T rows"
    assert h["bad_same"] == 0, "a benchmark contained SAME-GAME rows"


def test_same_game_exclusion_is_not_load_bearing_for_the_result(audit):
    """The directed rate must be essentially unchanged when same-game prior
    rows are kept (audit B2 variant ii) — the finding is a property of the
    archive, not of the exclusion."""
    b2 = audit["b2"]
    assert b2["directed"][2] == pytest.approx(PRIMARY_UNDER_PCT_FROZEN,
                                              abs=PRIMARY_TOLERANCE_PP)
    assert b2["max_variant_delta"] <= MAX_VARIANT_DELTA_PP, \
        "construction variants moved the primary rate more than the frozen band"


# ── AUDIT I — production pace_benchmark_cache direction (read-only) ─────

def test_production_benchmark_cache_never_larger_than_prior_population(audit):
    """Leakage-direction check on the LIVE cache: a cached population LARGER
    than the strictly-prior rebuild of the same cutoff would mean rows from
    at/after the cutoff leaked in.  Every reproduced mismatch must be the
    benign direction (rebuilt >= cache)."""
    ires = audit["ires"]
    assert ires["pbc"]["mism"] + ires["pbc"]["matched"] > 0, \
        "no cache rows could be reproduced at all"
    for ex in ires["pbc"]["examples"]:
        cache_n, rebuilt_n = ex[3][0], ex[4][0]
        assert rebuilt_n >= cache_n, (
            "leakage direction detected: cache population larger than the "
            f"strictly-prior rebuild (key={ex[0]} cutoff={ex[1]})")
