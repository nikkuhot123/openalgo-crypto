#!/usr/bin/env python
"""Run-loop smoke tests for strategies/examples/prior_levels_ema_strategy.py.

Overnight mode end-to-end: entry on a strong-bull close at 15:05+, the CARRY
night, spot/target management, next-open exit the following morning; the
expiry-day stand-down; and the rejected-exit retry (no re-entry, position kept).
The broker is scripted; the module clock is patched; no network.
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


class _Stop(BaseException):
    """Escapes run_strategy's while loop to end the test."""


def _frame(crash=False):
    """Prior day flat at 25300 (PDH/PDL 25300); today rising to 25330 at 14:45,
    then a 15:00 partial at 25320 (no spot-stop on entry night unless `crash`,
    which drops the tail to 25000 -- well through the 0.2% stop)."""
    rows, idx = [], []
    d1 = TODAY - timedelta(days=1)
    t0 = datetime.combine(d1, dtime(9, 15))
    for b in range(23):
        for m in range(15):
            rows.append((25300, 25300, 25300, 25300, 1000))
            idx.append(t0 + timedelta(minutes=15 * b + m))
    t0 = datetime.combine(TODAY, dtime(9, 15))
    for b in range(23):
        c = 25000 + 15 * b  # 25000 .. 25330
        for m in range(15):
            rows.append((c, c, c, c, 1000))
            idx.append(t0 + timedelta(minutes=15 * b + m))
    tail = 25000 if crash else 25320
    for m in range(6):  # 15:00-15:05 partial
        rows.append((tail, tail, tail, tail, 1000))
        idx.append(t0 + timedelta(minutes=23 * 15 + m))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


class Broker:
    """Scripted OpenAlgo client: tracks positions, records every order."""

    def __init__(self, reject_sell=False):
        self.spot = 25320.0
        self.opt_ltp = 100.0
        self.reject_sell = reject_sell
        self.stop_out = False
        self.qty = 0
        self.orders = []

    def quotes(self, symbol, exchange):
        return {"status": "success", "data": {"ltp": self.opt_ltp}}

    def history(self, symbol, exchange, interval, start_date, end_date):
        return _frame(crash=self.stop_out)

    def expiry(self, symbol, exchange, instrumenttype):
        return {"status": "success", "data": ["07-AUG-26"]}

    def optionsymbol(self, **kw):
        return {"status": "success", "data": {"symbol": OPT, "lotsize": 75}}

    def funds(self):
        return {"status": "success", "data": {"availablecash": 200000}}

    def positionbook(self):
        data = [{"symbol": OPT, "quantity": self.qty, "average_price": 100.0}] if self.qty else []
        return {"status": "success", "data": data}

    def orderstatus(self, order_id, strategy=None):
        # Real SDK: orderstatus returns data as a DICT. The old fake exposed
        # `orderhistory` returning a LIST -- a method the SDK does not have --
        # so it validated the strategy's typo instead of catching it.
        return {"status": "success", "data": {"average_price": self.opt_ltp}}

    def tradebook(self):
        return {"status": "success", "data": []}

    def placeorder(self, **kw):
        self.orders.append(kw)
        if kw["transaction_type"] == "SELL" and kw["price_type"] == "MARKET":
            if self.reject_sell:
                return {"status": "error", "message": "RMS rejected"}
            self.qty -= int(kw["quantity"])
        elif kw["transaction_type"] == "SELL":  # protective SL-LIMIT: pending
            pass
        else:
            self.qty += int(kw["quantity"])
        return {"status": "success", "orderid": f"O{len(self.orders)}"}

    def cancelorder(self, **kw):
        return {"status": "success"}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Patch the module's clock, broker, state/lock dirs, sizes; return (broker, clock)."""
    broker = Broker()

    class FakeDT(datetime):
        _now = datetime.combine(TODAY, dtime(15, 7))

        @classmethod
        def now(cls, tz=None):
            return cls._now

    class FakeDate(date):
        @classmethod
        def today(cls):
            return FakeDT._now.date()

    monkeypatch.setattr(pl, "client", broker)
    monkeypatch.setattr(pl, "datetime", FakeDT)
    monkeypatch.setattr(pl, "date", FakeDate)
    monkeypatch.setattr(pl, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(pl, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(pl, "DRY_RUN", False)
    monkeypatch.setattr(pl, "QUANTITY", 75)
    monkeypatch.setattr(pl, "LOT_SIZE", 75)
    monkeypatch.setattr(pl, "_active_trade", {})
    monkeypatch.setattr(pl, "_day_state", {})
    monkeypatch.setattr(pl.time, "sleep", lambda s: None)
    return broker, FakeDT


def drive(rig, script):
    """Run run_strategy; apply script[i] (a callable or None) before the i-th tick."""
    broker, clock = rig
    steps = iter(script)

    def fake_sleep(secs):
        step = next(steps, None)
        if step is not None:
            step(broker)
        clock._now = clock._now + timedelta(seconds=secs)

    orig = pl.time.sleep
    pl.time.sleep = fake_sleep
    try:
        with pytest.raises(_Stop):
            pl.run_strategy()
    finally:
        pl.time.sleep = orig
    return broker


def test_overnight_entry_and_next_open_exit(rig):
    broker, clock = rig

    def step2(broker):
        # next morning 09:31: exit window (entry day + 1 >= EXIT_TIME 09:30)
        clock._now = clock._now.replace(
            year=TODAY.year, month=TODAY.month, day=TODAY.day
        ) + timedelta(days=1)
        clock._now = clock._now.replace(hour=9, minute=31)

    def step3(broker):
        raise _Stop()

    # step2 runs at sleep #2; the exit's fill-poll burns 5 sleeps; stop at #8
    broker = drive(rig, [None, step2] + [None] * 5 + [step3])
    buys = [o for o in broker.orders if o["transaction_type"] == "BUY"]
    sells = [
        o for o in broker.orders if o["transaction_type"] == "SELL" and o["price_type"] == "MARKET"
    ]
    assert len(buys) == 1 and int(buys[0]["quantity"]) == 75
    assert len(sells) == 1
    assert broker.qty == 0
    # position and direction lock released; the day's entry counter survives so
    # a restart cannot buy a second lot in the same session
    assert not list(Path(pl.LOCKS_DIR).glob("*.dir"))
    trade, day = pl.load_state()
    assert trade == {}
    assert day == {str(TODAY): 1}
    assert not pl.day_budget_left(day, TODAY)


def test_stopped_out_position_does_not_re_enter_the_same_session(rig):
    """Regression: the entry window stays open after a stop-out, so without the
    per-session budget the loop bought a fresh lot on the very next tick."""
    broker, clock = rig

    def crash_spot(broker):
        # drive spot through the 0.2% stop while the 15:05+ entry window is open
        broker.stop_out = True

    def stop(broker):
        raise _Stop()

    drive(rig, [None, crash_spot] + [None] * 6 + [stop])
    buys = [o for o in broker.orders if o["transaction_type"] == "BUY"]
    assert len(buys) == 1, "re-entered after the stop-out inside the same session"
    assert pl._active_trade == {}


def test_expiry_day_stands_down(rig, monkeypatch):
    broker, _ = rig
    monkeypatch.setattr(pl, "_expiry_present", lambda now: True)

    def stop(broker):
        raise _Stop()

    drive(rig, [None, None, stop])
    assert broker.orders == []  # no entry on expiry day
    assert pl._active_trade == {}


def test_rejected_exit_keeps_position_and_retries(rig):
    broker, clock = rig
    broker.reject_sell = True

    def morning(broker):
        clock._now = clock._now.replace(hour=9, minute=31)
        clock._now = clock._now + timedelta(days=1)

    def check(broker):
        raise _Stop()

    drive(rig, [None, morning, None, None, check])
    # entry happened, every market-sell attempt was rejected, position intact
    assert broker.qty == 75
    assert pl._active_trade.get("symbol") == OPT
    mkt_sells = [
        o for o in broker.orders if o["transaction_type"] == "SELL" and o["price_type"] == "MARKET"
    ]
    assert len(mkt_sells) >= 2  # rejected and retried, never re-entered
    assert len([o for o in broker.orders if o["transaction_type"] == "BUY"]) == 1
