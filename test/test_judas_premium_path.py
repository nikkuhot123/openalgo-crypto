#!/usr/bin/env python
"""Premium-path instrumentation in judas_swing_strategy.log_premium_path.

This collector is load-bearing for a decision, not just a log line. The exit
study needs >=15 trades of premium paths, and 2026-08-07 showed what a silent
no-op costs: prior_levels_ema ran green for days while calling two SDK methods
that do not exist, entering nothing. A collector that quietly fails would burn
the entire two-week window before anyone noticed.

So these tests pin:
  - it actually writes a PATH line, with the right numbers
  - the throttle holds (and is not so eager it floods, nor so lazy it starves)
  - a broker failure NEVER propagates -- instrumentation must not break an exit
  - a missing entry premium is handled rather than raising
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

TRADE = {"entry_opt_price": 127.50}


@pytest.fixture(autouse=True)
def _reset():
    judas._last_premium_log = 0.0
    yield
    judas._last_premium_log = 0.0


def _capture(monkeypatch, premium):
    """Point fetch_option_ltp at a fixed premium; collect emitted log lines."""
    lines = []
    if isinstance(premium, Exception):
        def _fetch(*a, **k):
            raise premium
    else:
        def _fetch(*a, **k):
            return premium
    monkeypatch.setattr(judas, "fetch_option_ltp", _fetch)
    monkeypatch.setattr(judas.log, "info", lambda m, *a, **k: lines.append(str(m)))
    return lines


def test_writes_a_path_line_with_the_real_numbers(monkeypatch):
    lines = _capture(monkeypatch, 148.50)
    assert judas.log_premium_path("NIFTY11AUG2624600PE", "NFO", TRADE, 24578.6, 65, now=1000.0)
    path = [x for x in lines if x.startswith("PATH ")]
    assert len(path) == 1
    # the 14:15 peak of the trade that motivated this: +16.5%, +Rs 1,365
    assert "prem=148.50" in path[0]
    assert "entry=127.50" in path[0]
    assert "pct=+16.5%" in path[0]
    assert "rs=+1365" in path[0]


def test_records_a_loss_with_sign(monkeypatch):
    lines = _capture(monkeypatch, 109.70)
    assert judas.log_premium_path("NIFTY11AUG2624600PE", "NFO", TRADE, 24557.3, 65, now=1000.0)
    # the 15:10 EOD exit: -14.0%, -Rs 1,157
    assert "pct=-14.0%" in lines[0]
    assert "rs=-1157" in lines[0]


def test_throttle_suppresses_the_second_call(monkeypatch):
    _capture(monkeypatch, 130.0)
    assert judas.log_premium_path("X", "NFO", TRADE, 1.0, 65, now=1000.0) is True
    assert judas.log_premium_path("X", "NFO", TRADE, 1.0, 65, now=1000.0) is False
    within = 1000.0 + judas.PREMIUM_LOG_SECS - 0.1
    assert judas.log_premium_path("X", "NFO", TRADE, 1.0, 65, now=within) is False


def test_throttle_reopens_after_the_interval(monkeypatch):
    _capture(monkeypatch, 130.0)
    assert judas.log_premium_path("X", "NFO", TRADE, 1.0, 65, now=1000.0) is True
    later = 1000.0 + judas.PREMIUM_LOG_SECS
    assert judas.log_premium_path("X", "NFO", TRADE, 1.0, 65, now=later) is True


def test_collects_often_enough_to_be_useful():
    """A path sampled once a minute cannot locate a peak worth acting on.
    Today's give-back developed over ~55 minutes."""
    assert 0 < judas.PREMIUM_LOG_SECS <= 60


def test_broker_failure_never_propagates(monkeypatch):
    _capture(monkeypatch, RuntimeError("broker down"))
    # must not raise: an exit decision follows this call in the run loop
    assert judas.log_premium_path("X", "NFO", TRADE, 1.0, 65, now=1000.0) is False


def test_missing_premium_is_not_an_error(monkeypatch):
    _capture(monkeypatch, None)
    assert judas.log_premium_path("X", "NFO", TRADE, 1.0, 65, now=1000.0) is False


def test_missing_entry_price_is_not_an_error(monkeypatch):
    _capture(monkeypatch, 130.0)
    assert judas.log_premium_path("X", "NFO", {}, 1.0, 65, now=1000.0) is False
    assert judas.log_premium_path("X", "NFO", None, 1.0, 65, now=2000.0) is False


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
