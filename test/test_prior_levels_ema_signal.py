#!/usr/bin/env python
"""Pure-function tests for strategies/examples/prior_levels_ema_strategy.py.

Covers the tiered PDH/PDL/PMH/PML bias, the EMA 9/21 gate, the pre-market
window extraction, the overnight-vs-intraday 15m bar selection, the expiry-day
detector, the entry geometry guard and the verified-exit outcome mapping.
No network: an openalgo stub is installed before import (the module builds an
`openalgo.api` client at import time).
"""

import os
import sys
import types
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategies" / "examples"))

_stub = types.ModuleType("openalgo")
_stub.api = lambda **kwargs: types.SimpleNamespace(**kwargs)
sys.modules.setdefault("openalgo", _stub)
os.environ.setdefault("OPENALGO_API_KEY", "test")

import prior_levels_ema_strategy as pl  # noqa: E402

TODAY = date(2026, 8, 5)
OPT = "NIFTY07AUG2625200CE"


def day_frame(day, bucket_closes, partial=None):
    """1m rows for one session: 15 bars per 15m bucket (09:15 + 15*b min),
    plus an optional partial 15:00 bucket (6 rows) to simulate a forming bar."""
    rows, idx = [], []
    t0 = datetime.combine(day, dtime(9, 15))
    for b, c in enumerate(bucket_closes):
        for m in range(15):
            rows.append((c, c, c, c, 1000))
            idx.append(t0 + timedelta(minutes=15 * b + m))
    if partial is not None:
        for m in range(6):
            rows.append((partial, partial, partial, partial, 1000))
            idx.append(t0 + timedelta(minutes=len(bucket_closes) * 15 + m))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


# ------------------------------------------------------------------ bias
def test_compute_bias_strong_levels():
    assert pl.compute_bias(
        25300, 25200, 24800, 25050, 24950, 25250, 25100, tiers="both", use_ema=True
    ) == ("CE", "strong_bull")
    assert pl.compute_bias(
        24700, 25200, 24800, 25050, 24950, 25100, 25250, tiers="both", use_ema=True
    ) == ("PE", "strong_bear")


def test_compute_bias_light_tier_between_pm_and_pdh():
    # close inside [PDL, PDH] but above PMH -> light bull (EMA aligned)
    assert pl.compute_bias(
        25100, 25200, 24800, 25050, 24950, 25250, 25100, tiers="both", use_ema=True
    ) == ("CE", "light_bull")
    # close inside the PM range -> neutral
    assert pl.compute_bias(
        25000, 25200, 24800, 25050, 24950, 25250, 25100, tiers="both", use_ema=True
    ) == (None, "neutral")
    # below PML but above PDL -> light bear
    assert pl.compute_bias(
        24900, 25200, 24800, 25050, 24950, 25100, 25250, tiers="both", use_ema=True
    ) == ("PE", "light_bear")


def test_compute_bias_ema_gate_blocks_misaligned():
    # price above PDH but EMA9 < EMA21 -> no signal
    assert pl.compute_bias(
        25300, 25200, 24800, 25050, 24950, 25100, 25250, tiers="both", use_ema=True
    ) == (None, "neutral")
    # gate off (SENSEX config): same levels fire immediately
    assert pl.compute_bias(
        25300, 25200, 24800, 25050, 24950, 25100, 25250, tiers="both", use_ema=False
    ) == ("CE", "strong_bull")


def test_compute_bias_tiers_strong_ignores_light():
    # light-bull geometry but tiers='strong' -> nothing
    assert pl.compute_bias(
        25100, 25200, 24800, 25050, 24950, 25250, 25100, tiers="strong", use_ema=True
    ) == (None, "neutral")
    assert pl.compute_bias(
        25300, 25200, 24800, 25050, 24950, 25250, 25100, tiers="strong", use_ema=True
    ) == ("CE", "strong_bull")


def test_compute_bias_no_ema_state_returns_none_when_gated():
    assert pl.compute_bias(
        25300, 25200, 24800, 25050, 24950, None, None, tiers="both", use_ema=True
    ) == (None, "neutral")
    # gate off tolerates missing EMAs
    assert pl.compute_bias(
        25300, 25200, 24800, 25050, 24950, None, None, tiers="both", use_ema=False
    ) == ("CE", "strong_bull")


def test_compute_bias_missing_levels_neutral():
    assert pl.compute_bias(25300, None, 24800, None, None, 25250, 25100) == (None, "neutral")


# ------------------------------------------------------------ data helpers
def test_pm_range_window():
    # half-open [09:15, 09:30): exactly the first 1m bucket (15 rows)
    df2 = day_frame(TODAY, [25000, 25100] + [25200] * 21)
    pmh, pml = pl.pm_range(df2, TODAY, dtime(9, 15), dtime(9, 30))
    assert (pmh, pml) == (25000.0, 25000.0)
    # intra-bucket extremes are captured
    rows, idx = [], []
    t0 = datetime.combine(TODAY, dtime(9, 15))
    for i in range(15):
        hi = 25000 + (i % 5) * 10  # highs 25000..25040
        lo = 25000 - (i % 7) * 10  # lows 25000..24940
        rows.append((25000, hi, lo, 25000, 1000))
        idx.append(t0 + timedelta(minutes=i))
    for i in range(10):  # outside the window: must be ignored
        rows.append((26000, 26100, 25900, 26000, 1000))
        idx.append(t0 + timedelta(minutes=16 + i))
    df3 = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)
    pmh, pml = pl.pm_range(df3, TODAY, dtime(9, 15), dtime(9, 30))
    assert (pmh, pml) == (25040.0, 24940.0)


def test_pm_range_before_window_returns_none():
    # a session whose bars start at 09:30 -> empty window
    rows, idx = [], []
    t0 = datetime.combine(TODAY, dtime(9, 30))
    for i in range(10):
        rows.append((25200, 25200, 25200, 25200))
        idx.append(t0 + timedelta(minutes=i))
    df2 = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    assert pl.pm_range(df2, TODAY, dtime(9, 15), dtime(9, 30)) == (None, None)


def test_fetch_prior_levels_last_completed_day():
    d1 = TODAY - timedelta(days=2)
    d2 = TODAY - timedelta(days=1)
    df = pd.concat(
        [
            day_frame(d1, [25000] * 23),
            day_frame(d2, [25300, 25100] + [25200] * 21),
            day_frame(TODAY, [25200] * 23),
        ]
    )
    pdh, pdl = pl.fetch_prior_levels(df, TODAY)
    assert (pdh, pdl) == (25300.0, 25100.0)  # d2's range, not d1's, not today's
    assert pl.fetch_prior_levels(day_frame(TODAY, [25200] * 23), TODAY) == (None, None)


def test_resample_15m_grid():
    closes = [25000 + 10 * b for b in range(23)]
    df = day_frame(TODAY, closes, partial=99999)
    d15 = pl.resample_15m(df)
    assert d15.index[0].time() == dtime(9, 15)
    assert d15.index[-1].time() == dtime(15, 0)  # the partial 15:00 bar exists
    assert list(d15["close"][:-1]) == [float(c) for c in closes]
    assert d15["close"].iloc[-1] == 99999.0


def test_compute_signal_overnight_excludes_forming_bar(monkeypatch):
    monkeypatch.setattr(pl, "USE_EMA", False)  # SENSEX-style: levels alone
    monkeypatch.setattr(pl, "TIERS", "both")
    d1 = TODAY - timedelta(days=1)
    # rising closes, last full bucket 14:45 closes at 25330 > PDH 25300
    closes = [25000 + 15 * b for b in range(23)]
    df = pd.concat([day_frame(d1, [25000] * 23), day_frame(TODAY, closes, partial=25200)])
    levels = {"pdh": 25300.0, "pdl": 25100.0, "pmh": 25000.0, "pml": 24900.0}
    side, tier, detail = pl.compute_signal(levels, df, mode="overnight")
    assert side == "CE"
    assert tier == "strong_bull"
    assert detail["close"] == 25330.0  # the 14:45 bar, NOT the 15:00 partial
    assert detail["pdh"] == 25300.0


def test_compute_signal_intraday_uses_latest_bar(monkeypatch):
    monkeypatch.setattr(pl, "USE_EMA", False)
    monkeypatch.setattr(pl, "TIERS", "both")
    closes = [25000 + 15 * b for b in range(23)]
    df = day_frame(TODAY, closes, partial=25200)
    levels = {"pdh": 25150.0, "pdl": 24900.0, "pmh": 25000.0, "pml": 24900.0}
    side, _, detail = pl.compute_signal(levels, df, mode="intraday")
    # the 15:00 partial bar (close 25200 > PDH) is the latest -> strong bull
    assert side == "CE"
    assert detail["close"] == 25200.0


def test_compute_signal_ema_alignment_overnight(monkeypatch):
    monkeypatch.setattr(pl, "USE_EMA", True)
    monkeypatch.setattr(pl, "TIERS", "both")
    closes = [25000 + 15 * b for b in range(23)]
    df = day_frame(TODAY, closes)
    levels = {"pdh": 25300.0, "pdl": 25100.0, "pmh": 25000.0, "pml": 24900.0}
    # 14:45 close 25330 > PDH, rising series -> EMA9 > EMA21 -> fires
    side, tier, detail = pl.compute_signal(levels, df, mode="overnight")
    assert side == "CE"
    assert detail["ema9"] > detail["ema21"]
    # flat series breaks the gate: close above PDH but EMAs equal
    flat = day_frame(TODAY, [25330.0] * 23)
    side, tier, _ = pl.compute_signal(levels, flat, mode="overnight")
    assert side is None


# ------------------------------------------------------------ misc helpers
def test_is_expiry_today():
    today = date.today()
    months = {
        1: "JAN",
        2: "FEB",
        3: "MAR",
        4: "APR",
        5: "MAY",
        6: "JUN",
        7: "JUL",
        8: "AUG",
        9: "SEP",
        10: "OCT",
        11: "NOV",
        12: "DEC",
    }
    fmt = f"{today.day:02d}{months[today.month]}{today.year % 100:02d}"
    assert pl.is_expiry_today(fmt)
    assert not pl.is_expiry_today("31DEC99")
    assert not pl.is_expiry_today("07-AUG-26")  # broker's dash format is not ours
    assert not pl.is_expiry_today("")


def test_check_entry_geometry():
    prem, qty = 100.0, 75
    entry, sl, tgt = 25000.0, 25000 * 0.998, 25000 * 1.004
    ok, why, detail = pl.check_entry_geometry(entry, sl, tgt, prem, qty)
    assert ok and why is None
    assert detail["effective_rr"] == pytest.approx(2.0, abs=0.01)
    # target too close to entry -> below breakeven guard
    ok, why, _ = pl.check_entry_geometry(entry, sl, entry + 1.0, prem, qty)
    assert not ok and why == "target-below-breakeven"
    # wide stop -> effective RR < 1.2
    ok, why, _ = pl.check_entry_geometry(entry, entry - 100.0, tgt, prem, qty)
    assert not ok and why == "effective-rr-too-low"
    ok, why, _ = pl.check_entry_geometry(entry, entry, tgt, prem, qty)
    assert not ok and why == "zero-risk"
    ok, why, _ = pl.check_entry_geometry(entry, sl, tgt, 0.0, qty)
    assert not ok and why == "no-breakeven"


def test_stop_state():
    trade = {"side": "CE", "entry_spot": 25000.0}
    assert pl._stop_state(trade, 25000 * 0.997) == "SL"
    assert pl._stop_state(trade, 25000 * 1.005) == "TGT"
    assert pl._stop_state(trade, 25020.0) is None
    assert pl._stop_state(trade, None) is None
    trade = {"side": "PE", "entry_spot": 25000.0}
    assert pl._stop_state(trade, 25000 * 1.003) == "SL"
    assert pl._stop_state(trade, 25000 * 0.995) == "TGT"
    assert pl._stop_state(trade, 25000 * 1.001) is None


# --------------------------------------------------- per-session entry budget
def test_day_budget_allows_one_entry_then_closes():
    day_state = {}
    day = date(2026, 8, 5)
    assert pl.entries_used(day_state, day) == 0
    assert pl.day_budget_left(day_state, day)
    pl.mark_entry(day_state, day)
    assert pl.entries_used(day_state, day) == 1
    assert not pl.day_budget_left(day_state, day)  # one trade/day, as backtested
    # a new session starts fresh and the stale counter is dropped
    nxt = date(2026, 8, 6)
    assert pl.day_budget_left(day_state, nxt)
    pl.mark_entry(day_state, nxt)
    assert list(day_state) == [str(nxt)]


def test_day_budget_tolerates_corrupt_state():
    assert pl.entries_used({"2026-08-05": "junk"}, date(2026, 8, 5)) == 0
    assert pl.entries_used(None, date(2026, 8, 5)) == 0


def test_day_budget_honours_a_raised_cap():
    day_state = {}
    day = date(2026, 8, 5)
    pl.mark_entry(day_state, day)
    assert not pl.day_budget_left(day_state, day, cap=1)
    assert pl.day_budget_left(day_state, day, cap=2)


def test_compute_signal_drops_the_bar_still_forming(monkeypatch):
    monkeypatch.setattr(pl, "USE_EMA", False)
    monkeypatch.setattr(pl, "TIERS", "both")
    closes = [25000 + 15 * b for b in range(23)]  # 14:45 bar closes 25330
    df = day_frame(TODAY, closes, partial=26000)  # forming 15:00 bar spikes
    levels = {"pdh": 25400.0, "pdl": 24900.0, "pmh": 25000.0, "pml": 24900.0}
    # without `now` the forming bar is visible and its spike fires a signal
    side, _, detail = pl.compute_signal(levels, df, mode="intraday")
    assert (side, detail["close"]) == ("CE", 26000.0)
    # at 15:05 the 15:00 bar has NOT closed -> the 14:45 close is the signal bar
    at_1505 = datetime.combine(TODAY, dtime(15, 5))
    side, tier, detail = pl.compute_signal(levels, df, mode="intraday", now=at_1505)
    assert detail["close"] == 25330.0
    assert (side, tier) == ("CE", "light_bull")  # 25330 sits between PMH and PDH
    # by 15:16 that bar is closed and visible again
    at_1516 = datetime.combine(TODAY, dtime(15, 16))
    _, _, detail = pl.compute_signal(levels, df, mode="intraday", now=at_1516)
    assert detail["close"] == 26000.0


def test_compute_signal_overnight_signal_bar_at_1505(monkeypatch):
    monkeypatch.setattr(pl, "USE_EMA", False)
    monkeypatch.setattr(pl, "TIERS", "both")
    closes = [25000 + 15 * b for b in range(23)]
    df = day_frame(TODAY, closes, partial=26000)
    levels = {"pdh": 25300.0, "pdl": 25100.0, "pmh": 25000.0, "pml": 24900.0}
    at_1505 = datetime.combine(TODAY, dtime(15, 5))
    side, tier, detail = pl.compute_signal(levels, df, mode="overnight", now=at_1505)
    assert (side, tier, detail["close"]) == ("CE", "strong_bull", 25330.0)


# ------------------------------------------------------- verified exit path
class ExitBroker:
    """Scripted client for the verified_exit_sell mapping."""

    def __init__(self, held, reject=False):
        self.held = held
        self.reject = reject
        self.sells = 0
        self.cancelled = 0

    def positionbook(self):
        rows = [{"symbol": OPT, "quantity": self.held, "average_price": 100.0}] if self.held else []
        return {"status": "success", "data": rows}

    def orderstatus(self, order_id, strategy=None):
        # Real SDK shape: data is a DICT, and the method is orderstatus.
        return {"status": "success", "data": {"average_price": 243.5}}

    def tradebook(self):
        return {"status": "success", "data": []}

    def placeorder(self, **kw):
        if kw["transaction_type"] == "SELL" and kw["price_type"] == "MARKET":
            self.sells += 1
            if self.reject:
                return {"status": "error", "message": "RMS rejected"}
        return {"status": "success", "orderid": f"O{self.sells}"}

    def cancelorder(self, **kw):
        self.cancelled += 1
        return {"status": "success"}


@pytest.mark.parametrize(
    "held,reject,expect",
    [
        (75, False, ("sold", 75, 243.5)),
        (0, False, ("flat", 0, None)),
        (75, True, ("rejected", 0, None)),
    ],
)
def test_verified_exit_sell_outcomes(monkeypatch, held, reject, expect):
    broker = ExitBroker(held, reject)
    monkeypatch.setattr(pl, "DRY_RUN", False)
    monkeypatch.setattr(pl, "client", broker)
    monkeypatch.setattr(pl.time, "sleep", lambda s: None)
    outcome = pl.verified_exit_sell("NIFTY", OPT, "NFO", 75, "SL1", "test")
    assert outcome == expect
    if held:
        assert broker.cancelled == 1


def test_verified_exit_sell_shadow_mode(monkeypatch):
    monkeypatch.setattr(pl, "DRY_RUN", True)
    outcome = pl.verified_exit_sell("NIFTY", OPT, "NFO", 75, "SL1", "test")
    assert outcome == ("sold", 75, None)
