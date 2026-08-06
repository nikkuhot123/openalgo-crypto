#!/usr/bin/env python
"""Tests for strategies/examples/red_bar_x_candle_strategy.py.

Pure-function tests over synthetic 5m OHLC plus stubbed-broker tests for the
exit-order outcome mapping and the symbol-lock staleness rules. No network.
The strategy module builds an `openalgo.api` client at import time, so a stub
module is installed in sys.modules before import.
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
CPR_LOW = {"cpp": 100.0, "top": 110.0, "bottom": 90.0}  # band under price -> CE allowed
CPR_HIGH = {"cpp": 99000.0, "top": 99100.0, "bottom": 98900.0}  # band over price -> PE allowed
CPR = CPR_LOW
PREV_CLOSE = 25000.0


def session(trigger, warmup_close=25000.0, x_rows=None, filler=None, prior_days=2):
    """Build a full frame: prior sessions (EMA warmup) + today's X candle + filler + trigger.

    A forming candle is appended because the engine drops the last row.
    """
    rows = []
    idx = []
    for d in range(prior_days, 0, -1):
        day = TODAY - timedelta(days=d)
        t0 = datetime.combine(day, datetime.strptime("09:15", "%H:%M").time())
        for i in range(40):
            rows.append((warmup_close,) * 4)
            idx.append(t0 + timedelta(minutes=5 * i))
    x_rows = x_rows or [(25000, 25100, 24900, 25000)] * 6  # X: 24900-25100
    t0 = datetime.combine(TODAY, datetime.strptime("09:15", "%H:%M").time())
    for i, r in enumerate(x_rows):
        rows.append(r)
        idx.append(t0 + timedelta(minutes=5 * i))
    # everything after the X candle starts at 09:45, whatever the X window holds
    t1 = datetime.combine(TODAY, datetime.strptime("09:45", "%H:%M").time())
    for i, r in enumerate(list(filler or []) + [trigger, (0, 0, 0, 0)]):
        rows.append(r)
        idx.append(t1 + timedelta(minutes=5 * i))
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)


# Filler bars that break the X range (killing the sideways gate) and drag both
# EMAs to the trade's side.
BREAK_UP = [(25100, 25200, 25090, 25190)] * 6
BREAK_DOWN = [(24900, 24910, 24800, 24810)] * 6


def sig(df, **kw):
    kw.setdefault("cpr", CPR)
    kw.setdefault("prev_close", PREV_CLOSE)
    return rb.compute_red_bar_signal(df, TODAY, kw["cpr"], kw["prev_close"])


# ------------------------------------------------------------------ signal
def test_levels_are_fib_of_the_x_candle():
    s = sig(session((25150, 25160, 25140, 25145), filler=BREAK_UP))
    assert (s["x_low"], s["x_high"]) == (24900, 25100)
    assert s["l44"] == pytest.approx(24900 + rb.FIB_LO * 200)
    assert s["l50"] == pytest.approx(25000.0)
    assert s["l56"] == pytest.approx(24900 + rb.FIB_HI * 200)


def test_red_bar_above_l56_buys_ce():
    # red bar (close < open) closing at 25145, above L56 = 25012
    s = sig(session((25190, 25195, 25130, 25145), filler=BREAK_UP))
    assert s["signal"] == "CE"
    # raw risk 15 pts is under the 0.10% floor (25.145), so the stop is floored
    assert s["risk"] == pytest.approx(25145 * rb.MIN_SL_PCT / 100.0)
    assert s["sl_spot"] == pytest.approx(25145 - s["risk"])


def test_red_bar_below_l44_buys_pe():
    s = sig(session((24900, 24905, 24850, 24860), filler=BREAK_DOWN), cpr=CPR_HIGH)
    assert s["signal"] == "PE"
    assert s["sl_spot"] == pytest.approx(24905.0)  # trigger bar high, above the floor
    assert s["target_spot"] == pytest.approx(24860 - rb.RR * 45)


def test_t1_and_target_use_the_same_floored_risk_as_the_stop():
    """A floored stop with an unfloored target silently collapses the real RR."""
    s = sig(session((25147, 25148, 25143, 25145), filler=BREAK_UP))
    assert s["signal"] == "CE"
    risk = s["risk"]
    assert risk == pytest.approx(25145 * rb.MIN_SL_PCT / 100.0)   # floor binds (raw = 2)
    assert s["sl_spot"] == pytest.approx(25145 - risk)
    assert s["t1_spot"] == pytest.approx(25145 + rb.T1_RR * risk)
    assert s["target_spot"] == pytest.approx(25145 + rb.RR * risk)
    # the realised reward:risk is the configured one, measured off the actual stop
    assert (s["target_spot"] - 25145) / (25145 - s["sl_spot"]) == pytest.approx(rb.RR)


def test_green_trigger_candle_is_rejected():
    s = sig(session((25130, 25195, 25125, 25190), filler=BREAK_UP))
    assert s["signal"] is None
    assert "not red" in s["reason"]


def test_close_inside_the_margin_is_rejected():
    # 25000 sits between L44 (24988) and L56 (25012) -- the fake-entry filter
    s = sig(session((25010, 25015, 24995, 25000), filler=BREAK_UP))
    assert s["signal"] is None
    assert "margin" in s["reason"]


def test_sideways_day_no_break_of_x_is_rejected():
    inside = [(25050, 25060, 25040, 25045)] * 6  # never exceeds 25100/24900
    s = sig(session((25060, 25065, 25030, 25040), filler=inside))
    assert s["signal"] is None
    assert "sideways" in s["reason"]


def test_ema_filter_blocks_a_counter_trend_call(monkeypatch):
    monkeypatch.setattr(rb, "REQUIRE_EMA10", True)
    monkeypatch.setattr(rb, "REQUIRE_EMA30", True)
    # prior sessions parked at 25600 keep both EMAs above the trigger's 25145 close
    s = sig(session((25190, 25195, 25130, 25145), warmup_close=25600.0, filler=BREAK_UP))
    assert s["signal"] is None
    assert "EMA" in s["reason"]


def test_cpr_gate_blocks_a_call_under_the_band(monkeypatch):
    monkeypatch.setattr(rb, "REQUIRE_CPR", True)
    high_cpr = {"cpp": 25300.0, "top": 25400.0, "bottom": 25200.0}
    s = sig(session((25190, 25195, 25130, 25145), filler=BREAK_UP), cpr=high_cpr)
    assert s["signal"] is None
    assert "CPR" in s["reason"]


def test_missing_cpr_fails_closed(monkeypatch):
    """No daily bar must refuse the trade, not silently drop the gate."""
    monkeypatch.setattr(rb, "REQUIRE_CPR", True)
    s = sig(session((25190, 25195, 25130, 25145), filler=BREAK_UP), cpr=None)
    assert s["signal"] is None
    assert "CPR unavailable" in s["reason"]


def test_missing_prev_close_fails_closed(monkeypatch):
    monkeypatch.setattr(rb, "REQUIRE_GAP_GATE", True)
    s = sig(session((25190, 25195, 25130, 25145), filler=BREAK_UP), prev_close=None)
    assert s["signal"] is None
    assert "gap gate cannot be evaluated" in s["reason"]


def test_gap_down_blocks_calls_until_half_the_gap_is_rebuilt(monkeypatch):
    monkeypatch.setattr(rb, "REQUIRE_GAP_GATE", True)
    # prev close 25400 vs open 25000 -> gap -400 (1.6%); 50% level = 25200
    s = sig(session((25190, 25195, 25130, 25145), filler=BREAK_UP), prev_close=25400.0)
    assert s["signal"] is None
    assert "gap-down" in s["reason"]


def test_oversized_trigger_bar_is_skipped():
    # 245-pt bar exceeds MAX_SL_PCT (0.60% of 25145 = 150.9 pts)
    s = sig(session((25400, 25405, 24900, 25145), filler=BREAK_UP))
    assert s["signal"] is None
    assert "too tall" in s["reason"]


def test_entry_window_closes_after_entry_end():
    late = [(25100, 25200, 25090, 25190)] * 60  # pushes past 14:30
    s = sig(session((25190, 25195, 25130, 25145), filler=late))
    assert s["signal"] is None
    assert "entry window" in s["reason"]


def test_levels_reanchor_to_the_1245_candle():
    # fill 09:45 -> 13:15 so the 12:45-13:15 block exists and has its own range
    pre = [(25100, 25200, 25090, 25190)] * 36  # 09:45 -> 12:45
    noon = [(25300, 25400, 25200, 25390)] * 6  # 12:45 -> 13:15 block
    s = sig(session((25390, 25395, 25330, 25345), filler=pre + noon))
    assert s["anchor"] == "12:45"
    assert (s["x_low"], s["x_high"]) == (25200, 25400)
    assert s["l56"] == pytest.approx(25200 + rb.FIB_HI * 200)


def test_anchor_needs_a_minimum_number_of_bars(monkeypatch):
    """On a 5m INTERVAL a feed gap must not turn 2 stray bars into the anchor."""
    monkeypatch.setattr(rb, "MIN_ANCHOR_BARS", 5)
    short_x = [(25000, 25100, 24900, 25000)] * 2  # only 2 of the 6 bars
    assert sig(session((25190, 25195, 25130, 25145), x_rows=short_x, filler=BREAK_UP)) is None


def test_index_and_option_exchanges_route_per_underlying():
    assert (rb._index_exchange("NIFTY"), rb._option_exchange("NIFTY")) == ("NSE_INDEX", "NFO")
    assert (rb._index_exchange("SENSEX"), rb._option_exchange("SENSEX")) == ("BSE_INDEX", "BFO")


# --------------------------------------------------------- exit-order safety
class FakeClient:
    """Minimal placeorder/cancelorder stub recording what was sent."""

    def __init__(self, order_status="success"):
        self.order_status = order_status
        self.orders = []

    def placeorder(self, **kw):
        self.orders.append(kw)
        return {"status": self.order_status, "orderid": "X1"}

    def cancelorder(self, **kw):
        return {"status": "success"}


@pytest.fixture
def broker(monkeypatch):
    fake = FakeClient()
    monkeypatch.setattr(rb, "client", fake)
    monkeypatch.setattr(rb, "fetch_fill_price", lambda oid, sym, **kw: 42.0)
    return fake


def test_exit_sell_reports_sold_and_caps_at_broker_qty(broker, monkeypatch):
    monkeypatch.setattr(rb, "live_position_qty", lambda u, s: 75)
    outcome, qty, fill = rb.verified_exit_sell("NIFTY", "N26000CE", "NFO", 150, None, "Target Hit")
    assert (outcome, qty, fill) == ("sold", 75, 42.0)
    assert broker.orders[-1]["quantity"] == 75      # never sells more than held
    assert broker.orders[-1]["action"] == "SELL"


def test_exit_sell_reports_flat_without_placing_an_order(broker, monkeypatch):
    monkeypatch.setattr(rb, "live_position_qty", lambda u, s: 0)
    outcome, qty, _ = rb.verified_exit_sell("NIFTY", "N26000CE", "NFO", 75, None, "Target Hit")
    assert (outcome, qty) == ("flat", 0)
    assert broker.orders == []                      # no naked short


def test_exit_sell_reports_unknown_when_the_book_is_unreadable(broker, monkeypatch):
    monkeypatch.setattr(rb, "live_position_qty", lambda u, s: None)
    outcome, qty, _ = rb.verified_exit_sell("NIFTY", "N26000CE", "NFO", 75, None, "Target Hit")
    assert (outcome, qty) == ("unknown", 0)
    assert broker.orders == []


def test_rejected_exit_is_not_reported_as_flat(monkeypatch):
    """The teardown path keys off this: 'rejected' must never look like 'flat',
    or a live position gets abandoned with its state file wiped."""
    fake = FakeClient(order_status="error")
    monkeypatch.setattr(rb, "client", fake)
    monkeypatch.setattr(rb, "live_position_qty", lambda u, s: 75)
    outcome, qty, fill = rb.verified_exit_sell("NIFTY", "N26000CE", "NFO", 75, None, "Stop-Loss Hit")
    assert outcome == "rejected"
    assert (qty, fill) == (0, None)


def test_premium_sl_without_an_orderid_counts_as_not_placed(monkeypatch):
    """Brokers answer success/orderid=null on rejection; that stop does not exist."""
    class NoIdClient(FakeClient):
        def placeorder(self, **kw):
            self.orders.append(kw)
            return {"status": "success", "orderid": None}

    fake = NoIdClient()
    monkeypatch.setattr(rb, "client", fake)
    oid, trigger = rb.place_premium_sl("N26000CE", "NFO", 75, 100.0)
    assert oid is None
    assert trigger == pytest.approx(round(100.0 * (1 - rb.PREMIUM_SL_PCT / 100.0) / 0.05) * 0.05)
    sent = fake.orders[-1]
    assert sent["price_type"] == "SL"               # SL-M is rejected 33/33 on NFO/BFO
    assert sent["price"] < sent["trigger_price"]    # limit must sit below the trigger


def test_statutory_cost_is_charged_on_both_legs():
    assert rb.statutory_cost(100.0, 120.0, 75) == pytest.approx(
        (100.0 + 120.0) * 75 * rb.OPT_COST_PCT / 100.0)
    assert rb.statutory_cost(None, 120.0, 75) == 0.0


# ------------------------------------------------------------------- locks
def test_a_live_lock_blocks_and_a_stale_one_is_reclaimed(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "LOCKS_DIR", tmp_path)
    lock = tmp_path / "N26000CE.lock"

    # held by a live process (our own pid) -> blocked
    lock.write_text(f"Other Strategy|{datetime.now().isoformat()}|{os.getpid()}")
    assert rb.acquire_symbol_lock("N26000CE", "Red Bar X-Candle") is False

    # yesterday's lock is stale regardless of pid
    lock.write_text(f"Other|{(datetime.now() - timedelta(days=1)).isoformat()}|{os.getpid()}")
    assert rb.acquire_symbol_lock("N26000CE", "Red Bar X-Candle") is True
    assert lock.read_text().split("|")[0] == "Red Bar X-Candle"


@pytest.mark.skipif(os.name != "posix",
                    reason="os.kill(pid, 0) only distinguishes dead pids on POSIX; "
                           "the deployment target is Linux")
def test_dead_owner_pid_makes_a_lock_reclaimable(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "LOCKS_DIR", tmp_path)
    lock = tmp_path / "N26000CE.lock"
    lock.write_text(f"Other Strategy|{datetime.now().isoformat()}|999999")
    assert rb.acquire_symbol_lock("N26000CE", "Red Bar X-Candle") is True
    assert lock.read_text().split("|")[0] == "Red Bar X-Candle"


def test_lock_payload_carries_the_pid_for_sibling_strategies(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "LOCKS_DIR", tmp_path)
    assert rb.acquire_symbol_lock("N26000PE", "Red Bar X-Candle") is True
    parts = (tmp_path / "N26000PE.lock").read_text().split("|")
    assert len(parts) == 3 and parts[2] == str(os.getpid())
    rb.release_symbol_lock("N26000PE", "Red Bar X-Candle")
    assert not (tmp_path / "N26000PE.lock").exists()


def test_direction_lock_blocks_the_opposite_side_from_another_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "LOCKS_DIR", tmp_path)
    (tmp_path / "NIFTY.HA_EMA34_Channel.CE.dir").write_text(
        f"{datetime.now().isoformat()}|{os.getpid()}")

    assert rb.acquire_direction_lock("NIFTY", "PE", "Red Bar X-Candle") is False
    assert rb.acquire_direction_lock("NIFTY", "CE", "Red Bar X-Candle") is True
    assert (tmp_path / "NIFTY.Red_Bar_X_Candle.CE.dir").exists()

    # never blocked by our own claim
    assert rb.acquire_direction_lock("NIFTY", "CE", "Red Bar X-Candle") is True
    rb.release_direction_lock("NIFTY", "Red Bar X-Candle")
    assert not list(tmp_path.glob("NIFTY.Red_Bar_X_Candle.*.dir"))


# --------------------------------------------------------------- regime gates
# The gates are what separate the profitable configuration from the losing one:
# on 2026-05-28..08-06 the same signal is -Rs 10,919 ungated and -Rs 896 gated,
# so a gate that silently passes everything ships the losing strategy.
def _daily(closes, end=TODAY):
    """Daily frame whose last row is the session BEFORE `end`."""
    idx = [pd.Timestamp(end) - timedelta(days=len(closes) - i) for i in range(len(closes))]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes,
                         "close": closes}, index=idx)


def _client_with(df, monkeypatch):
    monkeypatch.setattr(rb, "client", types.SimpleNamespace(
        history=lambda **kw: df))


def test_gate_stands_down_on_skipped_weekday(monkeypatch):
    _client_with(_daily([100.0] * 8), monkeypatch)
    tuesday = date(2026, 8, 4)
    assert tuesday.weekday() == 1
    ok, why = rb.regime_gate("NIFTY", "NSE_INDEX", tuesday)
    assert ok is False and "weekday" in why


def test_gate_blocks_after_a_strong_five_session_run_up(monkeypatch):
    # +3% over the five sessions ending yesterday -> above the 0.0137 cutoff
    _client_with(_daily([100.0, 100.0, 100.0, 101.0, 102.0, 103.0]), monkeypatch)
    ok, why = rb.regime_gate("NIFTY", "NSE_INDEX", TODAY)
    assert ok is False and "mom5" in why


def test_gate_allows_a_flat_or_falling_five_sessions(monkeypatch):
    _client_with(_daily([100.0, 100.0, 100.0, 100.0, 99.5, 99.0]), monkeypatch)
    ok, why = rb.regime_gate("NIFTY", "NSE_INDEX", TODAY)
    assert ok is True and "clear" in why


def test_gate_uses_yesterdays_close_not_todays(monkeypatch):
    """mom5_prev must ignore any bar dated today -- that would be lookahead."""
    df = _daily([100.0, 100.0, 100.0, 100.0, 100.0, 100.2])
    today_row = pd.DataFrame({"open": [130.0], "high": [130.0], "low": [130.0],
                              "close": [130.0]}, index=[pd.Timestamp(TODAY)])
    _client_with(pd.concat([df, today_row]), monkeypatch)
    ok, why = rb.regime_gate("NIFTY", "NSE_INDEX", TODAY)
    # +0.2% over the prior five sessions passes; today's +30% spike must not count
    assert ok is True, why


def test_gate_fails_closed_when_history_is_short_or_broken(monkeypatch):
    _client_with(_daily([100.0, 101.0]), monkeypatch)
    assert rb.regime_gate("NIFTY", "NSE_INDEX", TODAY)[0] is False

    def boom(**kw):
        raise RuntimeError("broker down")
    monkeypatch.setattr(rb, "client", types.SimpleNamespace(history=boom))
    ok, why = rb.regime_gate("NIFTY", "NSE_INDEX", TODAY)
    assert ok is False and "failing closed" in why


# ----------------------------------------------------------------- shadow mode
def test_shadow_mode_takes_no_locks_and_sends_no_orders(tmp_path, monkeypatch):
    monkeypatch.setattr(rb, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(rb, "DRY_RUN", True)

    # a foreign strategy holding the opposite direction must not block shadow
    (tmp_path / "NIFTY.HA_EMA34_Channel.CE.dir").write_text(
        f"{datetime.now().isoformat()}|{os.getpid()}")
    assert rb.acquire_direction_lock("NIFTY", "PE", "Red Bar Shadow") is True
    assert rb.acquire_symbol_lock("NIFTY26000PE", "Red Bar Shadow") is True
    # and it must leave no lock behind for the live instance to trip over
    assert not (tmp_path / "NIFTY26000PE.lock").exists()
    assert not list(tmp_path.glob("NIFTY.Red_Bar_Shadow.*.dir"))

    def explode(**kw):
        raise AssertionError("shadow mode placed a real order")
    monkeypatch.setattr(rb, "client", types.SimpleNamespace(
        placeorder=explode, quotes=lambda **kw: {"status": "success", "data": {"ltp": 120.0}}))

    oid, trig = rb.place_premium_sl("NIFTY26000PE", "NFO", 65, 100.0)
    assert oid == "shadow-sl" and trig == 30.0          # 70% below entry

    outcome, qty, px = rb.verified_exit_sell("NIFTY", "NIFTY26000PE", "NFO", 65,
                                             oid, "Target Hit")
    assert (outcome, qty, px) == ("sold", 65, 120.0)


def test_live_mode_still_places_orders_and_takes_locks(tmp_path, monkeypatch):
    """DRY_RUN must default off: the live path is unchanged."""
    monkeypatch.setattr(rb, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(rb, "DRY_RUN", False)
    assert rb.acquire_symbol_lock("NIFTY26000PE", "Red Bar X-Candle") is True
    assert (tmp_path / "NIFTY26000PE.lock").exists()
    rb.release_symbol_lock("NIFTY26000PE", "Red Bar X-Candle")


def test_shadow_state_file_is_separate_from_live(monkeypatch):
    """A shadow snapshot must never be adopted as a real position on restart."""
    import importlib
    monkeypatch.setenv("DRY_RUN", "true")
    shadow = importlib.reload(rb)
    shadow_path = shadow.STATE_FILE
    monkeypatch.setenv("DRY_RUN", "false")
    live = importlib.reload(rb)
    assert shadow_path != live.STATE_FILE
    assert "shadow" in shadow_path.name and "shadow" not in live.STATE_FILE.name
