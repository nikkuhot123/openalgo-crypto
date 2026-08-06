#!/usr/bin/env python
"""Run-loop smoke tests for strategies/examples/red_bar_x_candle_strategy.py.

Drives `run_strategy()` end to end against a scripted fake broker and a fake
clock: entry -> T1 half-book -> stop-to-cost -> target exit, and the failure
path where the exit order is rejected. No network, no real time.
"""

import os
import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategies" / "examples"))

_stub = types.ModuleType("openalgo")
_stub.api = lambda **kwargs: types.SimpleNamespace(**kwargs)
sys.modules.setdefault("openalgo", _stub)
os.environ.setdefault("OPENALGO_API_KEY", "test")

import red_bar_x_candle_strategy as rb  # noqa: E402

TODAY = date(2026, 8, 5)
OPT = "NIFTY07AUG2625200CE"


class _Stop(BaseException):
    """Escapes run_strategy's `except Exception` to end the loop."""


def _intraday_frame():
    """Prior-day warmup + X candle 24900-25100 + upside break + a red trigger bar."""
    rows, idx = [], []
    for d in (2, 1):
        t0 = datetime.combine(TODAY - timedelta(days=d), datetime.min.time()).replace(hour=9, minute=15)
        for i in range(40):
            rows.append((25000.0,) * 4)
            idx.append(t0 + timedelta(minutes=5 * i))
    t0 = datetime.combine(TODAY, datetime.min.time()).replace(hour=9, minute=15)
    for i in range(6):                                    # X candle
        rows.append((25000, 25100, 24900, 25000))
        idx.append(t0 + timedelta(minutes=5 * i))
    t1 = datetime.combine(TODAY, datetime.min.time()).replace(hour=9, minute=45)
    body = [(25100, 25200, 25090, 25190)] * 6             # breaks the X range, lifts the EMAs
    body.append((25190, 25195, 25130, 25145))             # red trigger closing above L56
    body.append((0, 0, 0, 0))                             # forming candle, dropped by the engine
    for i, r in enumerate(body):
        rows.append(r)
        idx.append(t1 + timedelta(minutes=5 * i))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


def _daily_frame():
    """Prior sessions: CPR band at 25000 (below the trade's entry) and a flat
    five-session run so the mom5_prev regime gate passes. The gate needs six
    prior closes and fails closed without them."""
    idx = [datetime.combine(TODAY - timedelta(days=d), datetime.min.time())
           for d in (8, 7, 6, 5, 4, 3, 2, 1)]
    return pd.DataFrame([(25000, 25050, 24950, 25000)] * len(idx),
                        columns=["open", "high", "low", "close"], index=idx)


class Broker:
    """Scripted OpenAlgo client: tracks positions, records every order."""

    def __init__(self, reject_sell=False):
        self.spot = 25145.0
        self.opt_ltp = 100.0
        self.reject_sell = reject_sell
        self.qty = 0
        self.orders = []

    # --- market data
    def quotes(self, symbol, exchange):
        ltp = self.spot if exchange.endswith("_INDEX") else self.opt_ltp
        return {"status": "success", "data": {"ltp": ltp}}

    def history(self, symbol, exchange, interval, start_date, end_date):
        return _daily_frame() if interval == "D" else _intraday_frame()

    # --- symbols
    def expiry(self, symbol, exchange, instrumenttype):
        return {"status": "success", "data": ["07-AUG-26"]}

    def optionsymbol(self, **kw):
        return {"status": "success", "symbol": OPT}

    def optionchain(self, **kw):
        return {"status": "success", "chain": [{"ce": {"lotsize": 75}}]}

    def funds(self):
        return {"status": "success", "data": {"availablecash": 200000}}

    # --- orders
    def positionbook(self):
        data = [{"symbol": OPT, "quantity": self.qty, "average_price": 100.0}] if self.qty else []
        return {"status": "success", "data": data}

    def placeorder(self, **kw):
        self.orders.append(kw)
        if kw["action"] == "SELL" and kw["price_type"] == "MARKET":
            if self.reject_sell:
                return {"status": "error", "message": "RMS rejected"}
            self.qty -= int(kw["quantity"])
        elif kw["action"] == "BUY":
            self.qty += int(kw["quantity"])
        return {"status": "success", "orderid": f"O{len(self.orders)}"}

    def cancelorder(self, **kw):
        return {"status": "success"}

    def orderstatus(self, order_id, strategy):
        return {"status": "success", "data": {"average_price": self.opt_ltp}}


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Patch the module's clock, broker, state file and lock dir; return the broker."""
    broker = Broker()

    class FakeDT(datetime):
        _now = datetime.combine(TODAY, datetime.min.time()).replace(hour=10, minute=20)

        @classmethod
        def now(cls, tz=None):
            return cls._now

    class FakeDate(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(rb, "client", broker)
    monkeypatch.setattr(rb, "datetime", FakeDT)
    monkeypatch.setattr(rb, "date", FakeDate)
    monkeypatch.setattr(rb, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(rb, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(rb, "QUANTITY", 0)
    monkeypatch.setattr(rb, "MAX_LOTS", 2)          # digest 2.11 needs >= 2 lots for T1
    monkeypatch.setattr(rb, "MAX_TRADES_PER_DAY", 1)
    return broker, FakeDT


def drive(rig, script):
    """Run the loop, applying `script[i]` (a callable) before the i-th sleep."""
    broker, clock = rig
    steps = iter(script)

    def fake_sleep(secs):
        clock._now = clock._now + timedelta(seconds=secs)
        step = next(steps, None)
        if step is None:
            raise _Stop
        step(broker)

    rb.time.sleep = fake_sleep
    with pytest.raises(_Stop):
        rb.run_strategy()
    return broker


def test_entry_partial_book_and_target_exit(rig):
    broker, _ = rig
    broker = drive(rig, [
        lambda b: setattr(b, "spot", 25175.0),   # after the entry: T1 (entry + 1R) is hit
        lambda b: setattr(b, "opt_ltp", 130.0),
        lambda b: setattr(b, "spot", 25230.0),   # then the 3R target
        lambda b: setattr(b, "opt_ltp", 160.0),
    ])

    kinds = [(o["action"], o["price_type"], int(o["quantity"])) for o in broker.orders]
    assert kinds[0] == ("BUY", "MARKET", 150)                # 2 lots x 75
    assert kinds[1] == ("SELL", "SL", 150)                   # broker-side premium stop
    assert ("SELL", "MARKET", 75) in kinds                   # T1 half-book
    assert ("SELL", "SL", 75) in kinds                       # stop re-armed on what is left
    assert kinds.count(("SELL", "MARKET", 75)) == 2          # half at T1, half at target
    assert broker.qty == 0

    # state torn down cleanly, locks released, the day is accounted for
    trade, day = rb.load_state()
    assert trade == {}
    assert day["trades_today"] == 1 and day["consecutive_losses"] == 0
    assert not list(Path(rb.LOCKS_DIR).glob("*.lock"))
    assert not list(Path(rb.LOCKS_DIR).glob("*.dir"))


def test_a_rejected_exit_keeps_the_position_and_never_re_enters(rig, monkeypatch):
    broker, _ = rig
    broker.reject_sell = True
    monkeypatch.setattr(rb, "MAX_TRADES_PER_DAY", 2)    # re-entry would be allowed if we lost state

    broker = drive(rig, [
        lambda b: setattr(b, "spot", 25100.0),   # below the stop -> exit attempt (rejected)
        lambda b: None,
        lambda b: None,
        lambda b: None,
    ])

    assert broker.qty == 150, "the long is still open at the broker"
    assert [o["action"] for o in broker.orders].count("BUY") == 1, "must not re-enter"

    # the snapshot and the lock survive, so a restart can still adopt and manage it
    trade, _day = rb.load_state()
    assert trade["symbol"] == OPT and trade["qty_open"] == 150
    assert trade["sl_spot"] is not None
    assert (Path(rb.LOCKS_DIR) / f"{OPT}.lock").exists()
