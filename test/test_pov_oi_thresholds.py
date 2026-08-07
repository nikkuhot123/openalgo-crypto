#!/usr/bin/env python
"""Per-underlying OI thresholds in pov_wall_squeeze_strategy.

POV traded NIFTY and stopped taking SENSEX trades after 2026-07-30. Both OI
gates were absolute constants sized for NIFTY, and SENSEX carries ~31x less
positive OI churn per 5-minute bar at the same moneyness, so the same numbers
gated the two books in opposite directions:

    pre-gate >= 50,000   NIFTY 71% pass   SENSEX 11%   (starved it)
    c2       <  30,000   NIFTY 26% pass   SENSEX 95%   (free point)

The second half matters as much as the first: unblocking the pre-gate alone
would have left SENSEX collecting c2 for free and reaching the 4/5 entry bar
on three of the remaining four conditions -- a looser strategy than the one
that works on NIFTY.

NIFTY's values must not move. It is the working book.
"""
import importlib
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


def load(monkeypatch, underlying, **env):
    monkeypatch.setenv("UNDERLYING", underlying)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mod = importlib.import_module("pov_wall_squeeze_strategy")
    return importlib.reload(mod)


def test_nifty_thresholds_are_unchanged(monkeypatch):
    for k in ("PRE_OI_MIN", "OI_ABS_THRESHOLD"):
        monkeypatch.delenv(k, raising=False)
    m = load(monkeypatch, "NIFTY")
    assert m.PRE_OI_MIN == 50000
    assert m.OI_ABS_THRESHOLD == 30000


def test_sensex_is_scaled_to_its_own_oi_regime(monkeypatch):
    for k in ("PRE_OI_MIN", "OI_ABS_THRESHOLD"):
        monkeypatch.delenv(k, raising=False)
    m = load(monkeypatch, "SENSEX")
    assert m.PRE_OI_MIN == 1600, "50k starves SENSEX: 11% pass vs NIFTY's 71%"
    assert m.OI_ABS_THRESHOLD == 550, "30k hands SENSEX c2 for free: 95% pass"


def test_both_gates_move_together(monkeypatch):
    """Fixing only the pre-gate would make SENSEX strictly easier to trigger."""
    for k in ("PRE_OI_MIN", "OI_ABS_THRESHOLD"):
        monkeypatch.delenv(k, raising=False)
    # reload() returns the SAME module object, so read the values out before
    # loading the other book -- holding the module would alias them.
    m = load(monkeypatch, "NIFTY")
    n_pre, n_c2 = m.PRE_OI_MIN, m.OI_ABS_THRESHOLD
    m = load(monkeypatch, "SENSEX")
    s_pre, s_c2 = m.PRE_OI_MIN, m.OI_ABS_THRESHOLD

    assert s_pre < n_pre and s_c2 < n_c2
    # comparable regime, not wildly apart
    assert 0.5 < ((s_pre / s_c2) / (n_pre / n_c2)) < 2.5


def test_unknown_underlying_falls_back_to_nifty(monkeypatch):
    for k in ("PRE_OI_MIN", "OI_ABS_THRESHOLD"):
        monkeypatch.delenv(k, raising=False)
    m = load(monkeypatch, "BANKNIFTY")
    assert m.PRE_OI_MIN == 50000 and m.OI_ABS_THRESHOLD == 30000


def test_env_overrides_win(monkeypatch):
    m = load(monkeypatch, "SENSEX", PRE_OI_MIN="9999", OI_ABS_THRESHOLD="4321")
    assert m.PRE_OI_MIN == 9999 and m.OI_ABS_THRESHOLD == 4321


@pytest.mark.parametrize("underlying,pos4,expected_block", [
    ("SENSEX", 41120, False),   # best SENSEX leg on 2026-08-07 -- was blocked
    ("SENSEX", 13060, False),
    ("SENSEX", 60, True),       # genuinely dead leg still blocked
    ("NIFTY", 62595, False),    # weakest NIFTY leg still passes
    ("NIFTY", 41120, True),     # below NIFTY's floor, as before
])
def test_pre_gate_against_real_measured_legs(monkeypatch, underlying, pos4, expected_block):
    for k in ("PRE_OI_MIN", "OI_ABS_THRESHOLD"):
        monkeypatch.delenv(k, raising=False)
    m = load(monkeypatch, underlying)
    assert (pos4 < m.PRE_OI_MIN) is expected_block
