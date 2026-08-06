#!/usr/bin/env python
"""Overnight-carry wiring for strategies/examples/prior_levels_ema_strategy.py.

The strategy's only profitable mode is OVERNIGHT: enter ~15:05, carry the gap,
exit at the next session's open. Two pieces of plumbing decide whether that can
happen at all, and both fail silently -- the strategy would run, log, and trade,
just never actually hold a position overnight:

  1. PRODUCT must be NRML in overnight mode. MIS is force-squared by the broker
     around 15:20-15:30.
  2. The SIGTERM handler must NOT square off in overnight mode. The platform
     stops scheduled strategies with process.terminate() at schedule_stop,
     which lands minutes after the 15:05 entry.
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


def load(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    mod = importlib.import_module("prior_levels_ema_strategy")
    return importlib.reload(mod)


def test_overnight_defaults_to_a_carry_forward_product(monkeypatch):
    monkeypatch.delenv("PRODUCT", raising=False)
    m = load(monkeypatch, MODE="overnight")
    assert m.MODE == "overnight"
    assert m.PRODUCT == "NRML", "MIS would be force-squared before the gap"


def test_intraday_still_uses_mis(monkeypatch):
    monkeypatch.delenv("PRODUCT", raising=False)
    m = load(monkeypatch, MODE="intraday")
    assert m.PRODUCT == "MIS"


def test_product_env_still_wins(monkeypatch):
    m = load(monkeypatch, MODE="overnight", PRODUCT="MIS")
    assert m.PRODUCT == "MIS"


def test_sigterm_does_not_square_off_an_overnight_carry(monkeypatch):
    monkeypatch.delenv("PRODUCT", raising=False)
    m = load(monkeypatch, MODE="overnight")

    sold = []
    monkeypatch.setattr(m, "verified_exit_sell",
                        lambda *a, **k: sold.append(a) or ("sold", 1, 1.0))
    saved = []
    monkeypatch.setattr(m, "persist_state", lambda t, d: saved.append(t))
    monkeypatch.setattr(m, "_active_trade",
                        {"symbol": "NIFTY11AUG2624600CE", "qty": 65, "sl_oid": "X1"})
    monkeypatch.setattr(m, "_opt_exchange", "NFO")

    with pytest.raises(SystemExit):
        m._graceful_shutdown(15, None)          # SIGTERM

    assert sold == [], "overnight carry must survive the scheduled stop"
    assert saved and saved[0]["symbol"] == "NIFTY11AUG2624600CE", \
        "state must be persisted so the next session adopts the carry"


def test_sigterm_still_flattens_in_intraday_mode(monkeypatch):
    monkeypatch.delenv("PRODUCT", raising=False)
    m = load(monkeypatch, MODE="intraday")

    sold = []
    monkeypatch.setattr(m, "verified_exit_sell",
                        lambda *a, **k: sold.append(a) or ("sold", 1, 1.0))
    monkeypatch.setattr(m, "persist_state", lambda t, d: None)
    monkeypatch.setattr(m, "_active_trade",
                        {"symbol": "NIFTY11AUG2624600CE", "qty": 65, "sl_oid": "X1"})
    monkeypatch.setattr(m, "_opt_exchange", "NFO")

    with pytest.raises(SystemExit):
        m._graceful_shutdown(15, None)

    assert len(sold) == 1, "an intraday position must still be flattened"


def test_sigterm_with_no_position_is_a_clean_exit(monkeypatch):
    m = load(monkeypatch, MODE="overnight")
    monkeypatch.setattr(m, "_active_trade", {})
    with pytest.raises(SystemExit):
        m._graceful_shutdown(15, None)


def test_per_symbol_defaults_match_the_backtested_configs(monkeypatch):
    """NIFTY exits 09:30 with the EMA gate; SENSEX exits 09:20 without it."""
    monkeypatch.delenv("USE_EMA", raising=False)
    monkeypatch.delenv("EXIT_TIME", raising=False)
    n = load(monkeypatch, MODE="overnight", UNDERLYING="NIFTY")
    assert n.USE_EMA is True and n.EXIT_TIME.strftime("%H:%M") == "09:30"

    monkeypatch.delenv("USE_EMA", raising=False)
    monkeypatch.delenv("EXIT_TIME", raising=False)
    s = load(monkeypatch, MODE="overnight", UNDERLYING="SENSEX")
    assert s.USE_EMA is False and s.EXIT_TIME.strftime("%H:%M") == "09:20"
