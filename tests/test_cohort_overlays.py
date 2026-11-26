"""COHORT OVERLAYS + PER-LAYER UNITS — additive, independent, strongest wins.

One test per required property. No live DB, no network.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from blm_v4.betting import cohort_select as sel
from blm_v4.betting.cohort_units import _sel_config_for


@dataclass
class FakeStore:
    values: dict

    def get_config(self, key, default=None):
        return self.values.get(key, default)


def test_layers_default_off():
    cfg = sel.layer_config(sel.LAYER_Z1)
    assert cfg["enabled"] is False


def test_combined_tier_beats_z2(monkeypatch):
    monkeypatch.setenv("AUTOBET_Z2_ENABLED", "true")
    monkeypatch.setenv("AUTOBET_Z2TCX_ENABLED", "true")
    match = {sel.LAYER_Z2: True, sel.LAYER_TCX: True}
    r = sel.resolve_layers(match)
    assert r["layer"] == sel.LAYER_Z2TCX
    assert r["label"] == "Z2+TC-X"


def test_weakest_match_when_only_z1_enabled(monkeypatch):
    monkeypatch.setenv("AUTOBET_Z1_ENABLED", "true")
    match = {sel.LAYER_Z1: True, sel.LAYER_Z2: True}
    r = sel.resolve_layers(match)
    assert r["layer"] == sel.LAYER_Z1   # Z2 disabled -> weaker wins


def test_disabled_layer_never_wins():
    r = sel.resolve_layers({sel.LAYER_TCX: True})
    assert r["layer"] is None
    assert r["units"] is None


def test_store_toggle_beats_env_and_is_read_live(monkeypatch):
    monkeypatch.setenv("AUTOBET_Z1_ENABLED", "true")
    off = FakeStore({sel.layer_enabled_key(sel.LAYER_Z1): "false"})
    r = sel.resolve_layers({sel.LAYER_Z1: True}, store=off)
    assert r["layer"] is None


def test_store_units_override(monkeypatch):
    monkeypatch.setenv("AUTOBET_Z2_ENABLED", "true")
    store = FakeStore({sel.layer_units_key(sel.LAYER_Z2): "2.5"})
    r = sel.resolve_layers({sel.LAYER_Z2: True}, store=store)
    assert r["units"] == 2.5


def test_no_match_returns_default_units():
    r = sel.resolve_layers({})
    assert r["units"] is None


@dataclass
class Cfg:
    stake_units: float = 1.0
    max_stake_per_bet: float = 1000.0


def test_unit_override_uses_copy_not_global():
    cfg = Cfg(stake_units=1.0)
    out = _sel_config_for(cfg, {"cohort_units": 3.0})
    assert out is not cfg
    assert out.stake_units == 3.0
    assert cfg.stake_units == 1.0


def test_unit_override_ignores_bad_value():
    cfg = Cfg(stake_units=1.0)
    assert _sel_config_for(cfg, {"cohort_units": "NaN"}) is cfg
    assert _sel_config_for(cfg, {}) is cfg
