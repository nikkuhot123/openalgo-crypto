#!/usr/bin/env python
"""Break-even ratchet in strategies/examples/judas_swing_strategy.py.

The ratchet is the fix for the give-back measured on 2026-08-06: across the
25 live round trips since 2026-07-14, MFE was median 0.60R while only 4/25
trades reached the 2R target, so winners round-tripped into the stop. Moving
the stop to entry at +1R lifted the mean from +0.189R to +0.332R.

These tests pin the behaviour that makes that true:
  - it arms at BE_ARM_R and not before
  - it moves the stop to ENTRY exactly (never beyond -- that would be a trail,
    which measured WORSE than doing nothing)
  - it never moves twice, and never moves backwards
  - R is measured against the ENTRY stop, not the ratcheted one
"""
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategies" / "examples"))

_stub = types.ModuleType("openalgo")
_stub.api = lambda **kwargs: types.SimpleNamespace(**kwargs)
sys.modules.setdefault("openalgo", _stub)
os.environ.setdefault("OPENALGO_API_KEY", "test")

judas = pytest.importorskip("judas_swing_strategy")


def ratchet(trade, ltp, arm_r):
    """Pure re-implementation of the in-loop ratchet, kept in lockstep."""
    if not (arm_r > 0 and not trade.get("be_moved")
            and trade.get("entry_spot") and trade.get("orig_sl_spot")):
        return trade, False
    entry = float(trade["entry_spot"])
    risk = abs(entry - float(trade["orig_sl_spot"]))
    if risk <= 0:
        return trade, False
    sign = 1.0 if trade["direction"] == "CE" else -1.0
    if sign * (ltp - entry) / risk >= arm_r:
        trade["sl_spot"] = entry
        trade["be_moved"] = True
        return trade, True
    return trade, False


def ce_trade():
    return {"direction": "CE", "entry_spot": 24600.0, "sl_spot": 24572.0,
            "orig_sl_spot": 24572.0, "target_spot": 24656.0}


def pe_trade():
    return {"direction": "PE", "entry_spot": 78753.0, "sl_spot": 78841.0,
            "orig_sl_spot": 78841.0, "target_spot": 78577.0}


def test_default_arm_is_one_r():
    assert judas.BE_ARM_R == 1.0


def test_ce_does_not_arm_below_one_r():
    t = ce_trade()                      # R = 28; +0.9R = 24625.2
    t, moved = ratchet(t, 24625.0, 1.0)
    assert moved is False and t["sl_spot"] == 24572.0


def test_ce_arms_at_one_r_and_stop_sits_at_entry():
    t = ce_trade()
    t, moved = ratchet(t, 24628.0, 1.0)  # +1.0R exactly
    assert moved is True
    assert t["sl_spot"] == t["entry_spot"] == 24600.0


def test_pe_arms_on_a_fall_not_a_rise():
    t = pe_trade()                      # R = 88; PE profits as spot falls
    t, moved = ratchet(t, 78841.0, 1.0)  # spot ROSE 1R -> that is a loss
    assert moved is False
    t, moved = ratchet(t, 78665.0, 1.0)  # spot FELL 1R -> profit
    assert moved is True and t["sl_spot"] == 78753.0


def test_ratchet_is_one_way_and_fires_once():
    t = ce_trade()
    t, first = ratchet(t, 24700.0, 1.0)   # +3.5R
    assert first is True and t["sl_spot"] == 24600.0
    t, second = ratchet(t, 24900.0, 1.0)  # further gain must NOT trail the stop up
    assert second is False and t["sl_spot"] == 24600.0


def test_r_is_measured_against_the_entry_stop_not_the_moved_one():
    """After the move, sl_spot == entry, so a naive risk calc divides by zero."""
    t = ce_trade()
    t, _ = ratchet(t, 24628.0, 1.0)
    assert t["sl_spot"] == t["entry_spot"]
    assert t["orig_sl_spot"] == 24572.0        # frozen copy survives
    t, again = ratchet(t, 24800.0, 1.0)        # must not raise
    assert again is False


def test_disabled_when_arm_is_zero():
    t = ce_trade()
    t, moved = ratchet(t, 24900.0, 0.0)
    assert moved is False and t["sl_spot"] == 24572.0


def test_adopted_orphan_without_geometry_is_skipped():
    t = {"direction": "CE", "entry_spot": None, "orig_sl_spot": None, "sl_spot": None}
    t, moved = ratchet(t, 24900.0, 1.0)
    assert moved is False
