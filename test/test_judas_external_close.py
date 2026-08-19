#!/usr/bin/env python
"""External-close reconciliation in judas_swing_strategy.

2026-08-19, LIVE MONEY: a manual square-off closed NIFTY25AUG2624050CE at
11:58. Judas was still logging "Monitoring Trade" at 12:16 — 18 minutes later —
because `live_position_qty` was called nowhere except inside `if
exit_triggered`. The position was a ghost: the symbol lock stayed held, the
session stayed blocked, and the +Rs 520 never reached the books or the circuit
breakers.

The fix must close the tracking on POSITIVE EVIDENCE only. Assuming a bare
positionbook miss meant "closed" is what cancelled the protective stops on two
live POV legs on 2026-08-14, so that mistake must not be repeated here.
"""

import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategies" / "examples"))

stub = types.ModuleType("openalgo")
stub.api = lambda **kwargs: types.SimpleNamespace(**kwargs)
sys.modules.setdefault("openalgo", stub)
os.environ.setdefault("OPENALGO_API_KEY", "test")

import judas_swing_strategy as js  # noqa: E402


TRADE = {
    "symbol": "NIFTY25AUG2624050CE",
    "direction": "CE",
    "qty": 130,
    "entry_opt_price": 163.65,
    "entry_fill_price": 163.75,
    "entry_orderid": "26081900137300",
}


class Broker:
    """Minimal broker double. `qty` None models an unverifiable positionbook."""

    def __init__(self, qty=0, entry_status="complete", sells=(), raise_on=()):
        self.qty = qty
        self.entry_status = entry_status
        self.sells = list(sells)
        self.raise_on = set(raise_on)

    def positionbook(self):
        if "positionbook" in self.raise_on:
            raise RuntimeError("boom")
        if self.qty is None:
            return {"status": "error"}
        return {"status": "success",
                "data": [{"symbol": TRADE["symbol"], "quantity": str(self.qty)}]}

    def orderstatus(self, order_id=None, strategy=None):
        if "orderstatus" in self.raise_on:
            raise RuntimeError("boom")
        if self.entry_status is None:
            return {"status": "error"}
        return {"status": "success", "data": {"order_status": self.entry_status}}

    def tradebook(self):
        if "tradebook" in self.raise_on:
            raise RuntimeError("boom")
        return {"status": "success", "data": list(self.sells)}

    def orderbook(self):
        if "orderbook" in self.raise_on:
            raise RuntimeError("boom")
        return {"status": "success", "data": {"orders": list(self.sells)}}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    js._ext_close_miss[0] = 0
    yield
    js._ext_close_miss[0] = 0


def _use(monkeypatch, broker):
    monkeypatch.setattr(js, "client", broker)


# ---------------------------------------------------------------- detection

def test_the_19_aug_scenario_is_detected(monkeypatch):
    """Broker flat, entry confirmed filled -> something else closed it."""
    _use(monkeypatch, Broker(qty=0, entry_status="complete"))
    closed, why = js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])
    assert closed is True
    assert "filled" in why


def test_position_still_open_is_not_closed(monkeypatch):
    _use(monkeypatch, Broker(qty=130, entry_status="complete"))
    assert js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])[0] is False


def test_rejected_entry_never_became_a_position(monkeypatch):
    _use(monkeypatch, Broker(qty=0, entry_status="rejected"))
    closed, why = js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])
    assert closed is True
    assert "rejected" in why


def test_cancelled_entry_never_became_a_position(monkeypatch):
    _use(monkeypatch, Broker(qty=0, entry_status="cancelled"))
    closed, why = js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])
    assert closed is True
    assert "cancelled" in why


def test_unverifiable_positionbook_decides_nothing(monkeypatch):
    """The 14-Aug lesson: no positionbook answer means NO action."""
    _use(monkeypatch, Broker(qty=None))
    for _ in range(10):
        assert js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])[0] is False


def test_positionbook_exception_decides_nothing(monkeypatch):
    _use(monkeypatch, Broker(qty=0, raise_on=("positionbook",)))
    assert js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])[0] is False


def test_undetermined_entry_requires_repeated_misses(monkeypatch):
    """One miss is not proof; three consecutive misses are."""
    _use(monkeypatch, Broker(qty=0, entry_status=None))
    assert js.EXT_CLOSE_MISS_LIMIT == 3
    assert js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])[0] is False
    assert js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])[0] is False
    closed, why = js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])
    assert closed is True
    assert "undetermined" in why


def test_miss_counter_resets_when_position_reappears(monkeypatch):
    """A flicker must not accumulate toward the limit."""
    b = Broker(qty=0, entry_status=None)
    _use(monkeypatch, b)
    js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])
    js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])
    b.qty = 130
    assert js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])[0] is False
    assert js._ext_close_miss[0] == 0
    b.qty = 0
    assert js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])[0] is False


def test_pending_entry_is_undetermined_not_dead(monkeypatch):
    _use(monkeypatch, Broker(qty=0, entry_status="trigger pending"))
    assert js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])[0] is False


def test_no_trade_or_symbol_is_never_closed(monkeypatch):
    _use(monkeypatch, Broker(qty=0))
    assert js.detect_external_close({}, "NIFTY", TRADE["symbol"])[0] is False
    assert js.detect_external_close(TRADE, "NIFTY", None)[0] is False


def test_missing_entry_orderid_falls_back_to_miss_counting(monkeypatch):
    """Adopted orphans have no entry id -- must still reconcile, just slower."""
    _use(monkeypatch, Broker(qty=0, entry_status="complete"))
    orphan = {"symbol": TRADE["symbol"], "qty": 65, "adopted": True}
    assert js.detect_external_close(orphan, "NIFTY", TRADE["symbol"])[0] is False
    assert js.detect_external_close(orphan, "NIFTY", TRADE["symbol"])[0] is False
    assert js.detect_external_close(orphan, "NIFTY", TRADE["symbol"])[0] is True


# ------------------------------------------------------------- exit pricing

def _sell(px, status="complete", key="average_price"):
    return {"symbol": TRADE["symbol"], "action": "SELL",
            "order_status": status, key: px}


def test_exit_price_from_tradebook(monkeypatch):
    _use(monkeypatch, Broker(sells=[_sell("167.75")]))
    assert js.find_external_exit_price(TRADE["symbol"], TRADE) == 167.75


def test_exit_price_ignores_the_buy_leg(monkeypatch):
    b = Broker(sells=[{"symbol": TRADE["symbol"], "action": "BUY",
                       "order_status": "complete", "average_price": "163.75"}])
    _use(monkeypatch, b)
    assert js.find_external_exit_price(TRADE["symbol"], TRADE) is None


def test_exit_price_ignores_other_symbols(monkeypatch):
    b = Broker(sells=[{"symbol": "NIFTY25AUG2624150CE", "action": "SELL",
                       "order_status": "complete", "average_price": "99.70"}])
    _use(monkeypatch, b)
    assert js.find_external_exit_price(TRADE["symbol"], TRADE) is None


def test_exit_price_ignores_unfilled_sells(monkeypatch):
    _use(monkeypatch, Broker(sells=[_sell("167.75", status="trigger pending")]))
    assert js.find_external_exit_price(TRADE["symbol"], TRADE) is None


def test_exit_price_falls_back_to_orderbook(monkeypatch):
    b = Broker(sells=[_sell("167.75")], raise_on=("tradebook",))
    _use(monkeypatch, b)
    assert js.find_external_exit_price(TRADE["symbol"], TRADE) == 167.75


def test_exit_price_none_when_broker_has_nothing(monkeypatch):
    _use(monkeypatch, Broker(sells=[]))
    assert js.find_external_exit_price(TRADE["symbol"], TRADE) is None


def test_exit_price_none_never_raises(monkeypatch):
    _use(monkeypatch, Broker(raise_on=("tradebook", "orderbook")))
    assert js.find_external_exit_price(TRADE["symbol"], TRADE) is None


def test_zero_price_is_not_accepted(monkeypatch):
    """qty-0 rows carry avg 0.00; booking on that would fake a total loss."""
    _use(monkeypatch, Broker(sells=[_sell("0.00"), _sell("167.75")]))
    assert js.find_external_exit_price(TRADE["symbol"], TRADE) == 167.75


# ------------------------------------------------------- the booked outcome

def test_19_aug_pnl_reconstructs_to_the_real_number(monkeypatch):
    """The trade the books lost: BUY 163.75 -> SELL ~167.75 on 130 qty.

    Broker reported +Rs 520 gross-ish; net must be that minus statutory cost,
    and it must be a PROFIT, so consecutive_losses would reset rather than
    increment.
    """
    _use(monkeypatch, Broker(qty=0, entry_status="complete", sells=[_sell("167.75")]))
    closed, _ = js.detect_external_close(TRADE, "NIFTY", TRADE["symbol"])
    assert closed is True
    exit_px = js.find_external_exit_price(TRADE["symbol"], TRADE)
    qty = TRADE["qty"]
    gross = (exit_px - TRADE["entry_fill_price"]) * qty
    net = gross - js.statutory_cost(TRADE["entry_fill_price"], exit_px, qty)
    assert round(gross, 2) == 520.0
    assert 0 < net < gross          # real cost deducted, still a win


def test_wiring_exists_in_the_monitor_loop():
    """Guard against the helpers existing but never being called -- the exact
    shape of the original bug (live_position_qty existed, unused)."""
    src = (ROOT / "strategies" / "examples" / "judas_swing_strategy.py").read_text(encoding="utf-8")
    # the CALL site, not the def -- anchor on the assignment
    call = "_closed, _detail = detect_external_close(active_trade, UNDERLYING, symbol)"
    assert call in src
    assert "find_external_exit_price(symbol" in src
    assert '"entry_orderid":' in src
    # must stand down, not keep monitoring
    tail = src.split(call, 1)[1]
    assert "release_symbol_lock" in tail[:3000]
    assert "persist_done(today)" in tail[:3000]
    assert "state = \"DONE\"" in tail[:3000]


def test_reconcile_is_throttled_not_every_cycle():
    """In-trade polling is 5s; an unthrottled positionbook call there would add
    ~12 API calls/min for the whole session."""
    src = (ROOT / "strategies" / "examples" / "judas_swing_strategy.py").read_text(encoding="utf-8")
    assert "RECON_SECS" in src
    assert js.RECON_SECS >= 5.0
    call = "_closed, _detail = detect_external_close(active_trade, UNDERLYING, symbol)"
    before = src.split(call, 1)[0][-400:]
    assert "_last_recon" in before and "RECON_SECS" in before


def test_throttle_state_is_mutable_holder():
    """A bare module int would need a `global` inside run(); the holder keeps
    the clock working without one (same bug class as the collector's _hhmm)."""
    assert isinstance(js._last_recon, list)
    assert isinstance(js._ext_close_miss, list)



# ------------------------------------------------- one-trade-per-day marker
# `state = "DONE"` is in-memory only, so before this a mid-session restart came
# up IDLE and could open a SECOND position on a day Judas had already traded.
# The external-close fix makes that reachable: standing down clears the trade
# and would otherwise leave nothing behind to say why.

@pytest.fixture
def state_file(tmp_path, monkeypatch):
    f = tmp_path / "judas_swing_NIFTY.json"
    monkeypatch.setattr(js, "STATE_FILE", f)
    return f


def test_done_marker_round_trips(state_file):
    from datetime import date
    js.persist_done(date(2026, 8, 19))
    assert js.load_done_date() == date(2026, 8, 19)


def test_no_marker_reads_as_none(state_file):
    assert js.load_done_date() is None
    js.persist_trade({})
    assert js.load_done_date() is None


def test_an_open_trade_snapshot_is_not_a_done_marker(state_file):
    js.persist_trade(TRADE)
    assert js.load_done_date() is None


def test_corrupt_marker_reads_as_none(state_file):
    state_file.write_text('{"done_date": "not-a-date"}')
    assert js.load_done_date() is None
    state_file.write_text("{ this is not json")
    assert js.load_done_date() is None


def test_marker_does_not_leak_a_phantom_symbol(state_file):
    """The boot adopt path keys off saved['symbol']; a marker must not supply one."""
    from datetime import date
    js.persist_done(date(2026, 8, 19))
    assert js.load_persisted_trade().get("symbol") is None
    assert js.load_persisted_trade().get("sl_spot") is None


def test_exit_sites_write_the_marker_not_a_bare_clear():
    src = (ROOT / "strategies" / "examples" / "judas_swing_strategy.py").read_text(encoding="utf-8")
    # both DONE-after-a-trade sites
    assert src.count("persist_done(today)") == 2
    ext = src.split("EXTERNAL CLOSE: %s no longer held", 1)[1][:600]
    assert "persist_done(today)" in ext and "persist_trade({})" not in ext
    nor = src.split("# Judas is one-trade-per-day — after any exit", 1)[1][:400]
    assert "persist_done(today)" in nor and "persist_trade({})" not in nor


def test_boot_reads_marker_before_clearing_it():
    """persist_trade({}) would erase the record the check depends on, so the
    read must come first and the marker must be rewritten, not dropped."""
    src = (ROOT / "strategies" / "examples" / "judas_swing_strategy.py").read_text(encoding="utf-8")
    # anchor on CODE, not the comment -- the comment mentions persist_trade({})
    boot = src.split("_done_on = load_done_date()", 1)[1][:900]
    assert boot.index("persist_done(_done_on)") < boot.index("persist_trade({})  # broker holds nothing")
    assert 'state = "DONE"' in boot
    assert "persist_done(_done_on)" in boot


def test_new_day_reset_still_clears(state_file):
    """Yesterday's marker must not stand down today."""
    src = (ROOT / "strategies" / "examples" / "judas_swing_strategy.py").read_text(encoding="utf-8")
    reset = src.split("New trading day initialized", 1)[0][-400:]
    assert "persist_trade({})" in reset
    from datetime import date, timedelta
    js.persist_done(date.today() - timedelta(days=1))
    assert js.load_done_date() != date.today()

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
