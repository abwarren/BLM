"""LOCK the live trading parameters: the box, the doc and the registry must agree.

Three things can drift apart, and each has bitten this deployment:

  the BOX       systemd drop-ins under ~/.config/systemd/user/blm-server.service.d
  the DOC       docs/LIVE_TRADING_PARAMETERS.md (what an operator reads)
  the REGISTRY  blm_v4.trade_cohorts.COHORTS (what the code calls a cohort)

The live gate is supposed to be a NAMED cohort, not an ad-hoc number. These tests
fail when any two of the three disagree, so a parameter cannot move on the box
without the record moving with it — and cannot be "cleaned up" back to a wider
gate without someone editing the doc on purpose.

Host-dependent tests SKIP on a machine with no drop-ins (CI, a fresh checkout).
The registry/doc tests always run.
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
DOC = REPO / "docs" / "LIVE_TRADING_PARAMETERS.md"

#: What the LIVE gate is, by name.  Changing this line is a deliberate act.
LIVE_COHORT = "TC-C"

#: The declared map the doc commits to (the doc's "parameters in force" table).
EXPECTED = {
    "TRADE_MIN_PACE_RATIO": "1.04",
    "TRADE_SHORTFALL_MARGIN": "0.50",
    "TRADE_EXEC_MIN_PRICE": "1.60",
    "TRADE_MOVE_FILTER_ENABLED": "0",
    "BET_REVALIDATE": "1",
    "BET_REVALIDATE_MAX_AGE_S": "120",
}


def _params_module():
    path = REPO / "scripts" / "blm_live_params.py"
    spec = importlib.util.spec_from_file_location("blm_live_params", path)
    assert spec is not None and spec.loader is not None, path
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _declared_or_skip():
    mod = _params_module()
    if not mod._dropin_files():
        pytest.skip("no blm-server drop-ins on this host")
    return mod


# ── the registry ────────────────────────────────────────────────────────
def test_the_live_gate_is_a_named_cohort():
    """The declared legs must equal a cohort in the registry, not a stray number."""
    from blm_v4.trade_cohorts import COHORTS

    c = COHORTS[LIVE_COHORT]
    assert float(EXPECTED["TRADE_MIN_PACE_RATIO"]) == c["pace"]
    assert float(EXPECTED["TRADE_SHORTFALL_MARGIN"]) == -c["shortfall"]
    assert float(EXPECTED["TRADE_EXEC_MIN_PRICE"]) == c["price"]


# ── the doc ─────────────────────────────────────────────────────────────
def test_the_doc_exists_and_records_every_live_key():
    text = DOC.read_text(encoding="utf-8")
    for key, value in EXPECTED.items():
        assert key in text, f"{key} is deployed but not recorded in {DOC.name}"
        assert value in text, f"{key}={value} is deployed but the doc disagrees"


def test_the_doc_names_the_live_cohort():
    text = DOC.read_text(encoding="utf-8")
    assert LIVE_COHORT in text
    assert re.search(r"TC-C\s+CORE", text), "the doc must spell out the legs"


# ── the box ─────────────────────────────────────────────────────────────
def test_the_declared_drop_ins_equal_the_documented_map():
    mod = _declared_or_skip()
    decl, _sources = mod.declared()
    for key, want in EXPECTED.items():
        assert decl.get(key) == want, (
            f"{key}: the box declares {decl.get(key)!r}, the doc says {want!r} — "
            f"either the drop-in or docs/LIVE_TRADING_PARAMETERS.md is wrong")


def test_the_widening_tcw_value_is_shadowed_not_effective():
    """97-trade-pace-tcw.conf still says 1.00; something later must override it.

    This is the trap the doc calls out: if the 98-* file is renamed or removed,
    the gate silently widens to TC-W and nothing else complains.
    """
    mod = _declared_or_skip()
    decl, sources = mod.declared()
    files = {p.name for p in mod._dropin_files()}
    if "97-trade-pace-tcw.conf" not in files:
        pytest.skip("the widening file is gone — nothing to shadow")
    assert len(sources.get("TRADE_MIN_PACE_RATIO", [])) >= 2, (
        "only one drop-in sets TRADE_MIN_PACE_RATIO — the 1.00 in "
        "97-trade-pace-tcw.conf is now the live value")
    assert decl["TRADE_MIN_PACE_RATIO"] == EXPECTED["TRADE_MIN_PACE_RATIO"]
    # and the file that wins must sort last, which is how systemd decides
    winner = sorted(sources["TRADE_MIN_PACE_RATIO"])[-1]
    assert winner.startswith("98-"), (
        f"{winner} wins the rung by filename order — keep the decision in a "
        f"98-* file so it beats the 97-* widening value")


def test_effective_env_matches_declared_when_the_service_runs():
    """A drop-in edited without a restart is drift: the file says one thing,
    the process does another. That is the state this test exists to catch."""
    mod = _declared_or_skip()
    pid = mod._main_pid()
    if pid is None:
        pytest.skip("blm-server is not running")
    decl, _ = mod.declared()
    eff = mod.effective(pid)
    for key, want in EXPECTED.items():
        assert eff.get(key) == want, (
            f"{key}: process has {eff.get(key)!r}, doc says {want!r} — "
            f"daemon-reload && systemctl --user restart blm-server")
        assert decl.get(key) == eff.get(key), (
            f"{key}: drop-in says {decl.get(key)!r}, process has {eff.get(key)!r} — "
            f"the file changed without a restart")
