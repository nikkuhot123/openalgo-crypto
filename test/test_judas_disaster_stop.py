#!/usr/bin/env python
"""Resting DISASTER stop for Judas.

Judas's real stop is a SPOT level held in-process. A crash or SIGKILL between
polls leaves the position unprotected -- POV's 2026-07-02 incident is the
realised cost (3 SENSEX PE legs, 3+ hours, -75-80%).

This is NOT the strategy's stop. Measured over 1,338 live (spot, premium)
samples across 8 contracts (wiki/research/judas_broker_stop.md), the spot->
premium mapping is far too unstable to translate the real stop: |dPrem/dSpot|
ranges 0.019-0.856, one contract has R^2 = 0.00, and a translated stop is
mis-placed by a median 14.3% of premium. So instead a WIDE backstop sits at
-60%, beyond the worst adverse excursion ever observed (-48.6%).
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
_stub.api = lambda **kw: types.SimpleNamespace(**kw)
sys.modules.setdefault("openalgo", _stub)
os.environ.setdefault("OPENALGO_API_KEY", "test")

import judas_swing_strategy as js  # noqa: E402

SRC = (ROOT / "strategies" / "examples" / "judas_swing_strategy.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------ level

def test_level_is_60pct_and_clears_the_worst_observed_excursion():
    """Worst adverse premium excursion measured on any normally-managed trade
    was -48.6%. -40% would have pre-empted the real stop on 2 of 8 contracts and
    -50% clears the worst by only 1.4pp."""
    assert js.DISASTER_STOP_PCT == 60.0
    assert js.DISASTER_STOP_PCT > 48.6


def test_it_is_stop_limit_never_sl_m():
    """SL-M is rejected outright for options -- measured 33/33 on POV -- and the
    API reported that as success with orderid=null, silently leaving positions
    with no stop."""
    assert 'price_type="SL"' in SRC
    assert 'price_type="SL-M"' not in SRC
    assert js.SL_LIMIT_BUFFER_PCT == 5.0


class Broker:
    def __init__(self, ok=True, oid="D1"):
        self.ok, self.oid, self.orders, self.cancels = ok, oid, [], []

    def placeorder(self, **kw):
        self.orders.append(kw)
        if not self.ok:
            return {"status": "error", "message": "rejected"}
        return {"status": "success", "orderid": self.oid}

    def cancelorder(self, **kw):
        self.cancels.append(kw)
        return {"status": "success"}


def test_trigger_sits_60pct_below_entry_and_limit_below_trigger(monkeypatch):
    b = Broker()
    monkeypatch.setattr(js, "client", b)
    oid = js.place_disaster_stop("NIFTY25AUG2624050CE", "NFO", 100.0, 65)
    assert oid == "D1"
    o = b.orders[0]
    assert o["action"] == "SELL"
    assert o["price_type"] == "SL"
    assert o["quantity"] == 65
    assert o["trigger_price"] == pytest.approx(40.0, abs=0.05)   # -60%
    assert o["price"] < o["trigger_price"]                        # limit below trigger
    assert o["price"] >= 0.05


def test_prices_are_tick_rounded(monkeypatch):
    b = Broker()
    monkeypatch.setattr(js, "client", b)
    js.place_disaster_stop("X", "NFO", 163.65, 65)
    o = b.orders[0]
    for px in (o["trigger_price"], o["price"]):
        assert abs(round(px / 0.05) * 0.05 - px) < 1e-6, f"{px} not on a 0.05 tick"


def test_unarmed_backstop_is_loud_not_silent(monkeypatch, caplog):
    """Silence here is how a position ends up unprotected while the log looks
    healthy -- the exact SL-M/orderid=null failure."""
    b = Broker(ok=False)
    monkeypatch.setattr(js, "client", b)
    with caplog.at_level("WARNING"):
        assert js.place_disaster_stop("X", "NFO", 100.0, 65) is None
    assert any("NOT ARMED" in r.message for r in caplog.records)


def test_skipped_when_premium_too_small(monkeypatch):
    b = Broker()
    monkeypatch.setattr(js, "client", b)
    assert js.place_disaster_stop("X", "NFO", 0.10, 65) is None   # trigger < 0.05
    assert b.orders == []


def test_disabled_by_zero(monkeypatch):
    b = Broker()
    monkeypatch.setattr(js, "client", b)
    monkeypatch.setattr(js, "DISASTER_STOP_PCT", 0.0)
    assert js.place_disaster_stop("X", "NFO", 100.0, 65) is None
    assert b.orders == []


# --------------------------------------------------- cancel / naked short

def test_safe_cancel_treats_terminal_as_success(monkeypatch):
    class T:
        def cancelorder(self, **kw):
            return {"status": "error", "message": "order already complete"}
    monkeypatch.setattr(js, "client", T())
    ok, msg = js.safe_cancel_order("D1")
    assert ok is True and "already terminal" in msg


def test_safe_cancel_reports_real_failure(monkeypatch):
    class T:
        def cancelorder(self, **kw):
            return {"status": "error", "message": "gateway down"}
    monkeypatch.setattr(js, "client", T())
    ok, _ = js.safe_cancel_order("D1")
    assert ok is False


def test_safe_cancel_survives_a_throw(monkeypatch):
    class T:
        def cancelorder(self, **kw):
            raise RuntimeError("boom")
    monkeypatch.setattr(js, "client", T())
    ok, msg = js.safe_cancel_order("D1")
    assert ok is False and "threw" in msg


def test_no_order_id_is_a_clean_noop():
    assert js.safe_cancel_order(None)[0] is True


def test_cancelled_on_every_exit_path():
    """An orphaned resting SELL is a NAKED SHORT once the position is gone."""
    for marker in ("disaster stop cancel on external close",
                   "disaster stop cancel on %s",
                   "shutdown flat",
                   "shutdown close"):
        assert marker in SRC, f"missing cancel path: {marker}"
    # at least: normal exit, external close, shutdown-flat, shutdown-close
    assert SRC.count("safe_cancel_order(") >= 4


def test_left_armed_when_position_state_is_unknown():
    """The one case it must NOT be cancelled: we are exiting without knowing
    whether a position survives us. That is its whole purpose."""
    seg = SRC[SRC.index("cannot verify {symbol} position"):]
    seg = seg[:seg.index("if broker_qty <= 0")]
    assert "LEAVING disaster stop" in seg
    assert "safe_cancel_order" not in seg


def test_left_armed_if_the_shutdown_close_fails():
    seg = SRC[SRC.index("Failed to close position on shutdown"):]
    assert "LEAVING disaster stop" in seg[:300]


def test_armed_from_the_actual_fill_not_the_quote():
    seg = SRC[SRC.index("Rest the wide backstop"):]
    seg = seg[:seg.index("Entered Trade!")]
    assert 'active_trade.get("entry_fill_price")' in seg
    assert "place_disaster_stop(" in seg
    assert "persist_trade(active_trade)" in seg      # oid must survive a restart


def test_oid_is_persisted_at_every_arming_site():
    """There are two arming sites -- fresh entry and restart adoption -- and the
    oid must survive a restart from BOTH, or a later restart cannot cancel a
    resting order it does not know about."""
    call = 'active_trade["disaster_oid"] = place_disaster_stop('
    sites = [i for i in range(len(SRC)) if SRC.startswith(call, i)]
    assert len(sites) == 2, f"expected entry + adoption arming sites, found {len(sites)}"
    for i in sites:
        seg = SRC[i:i + 900]
        assert "persist_trade(active_trade)" in seg, \
            f"arming site at {i} does not persist the oid"


def test_real_stop_is_untouched():
    """The spot stop and the break-even ratchet must be unchanged -- this is a
    backstop, not a replacement."""
    assert js.BE_ARM_R == 1.0
    assert "BREAK-EVEN ARMED" in SRC
    assert "sl_spot" in SRC


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ============================================== recovery path (review 2)
# The pattern found reviewing all three strategies: protection present on the
# happy path, absent on the recovery path.

def test_adoption_verifies_or_rearms_the_backstop():
    """The restored-context branch carries a disaster_oid that may long since
    have been cancelled or filled, and the unknown-orphan branch builds a fresh
    dict with no oid at all -- so an adopted position would carry no backstop
    while the code believed otherwise."""
    seg = SRC[SRC.index("Adopt orphan position on boot"):]
    seg = seg[:seg.index("trade_date = date.today()")]
    assert "orderstatus(order_id=_d_oid" in seg, "must check whether it still rests"
    assert "trigger pending" in seg
    assert "place_disaster_stop(" in seg, "must re-arm when it is not live"


def test_adoption_warns_when_it_cannot_arm_a_backstop():
    seg = SRC[SRC.index("Adopt orphan position on boot"):]
    seg = seg[:seg.index("trade_date = date.today()")]
    assert "NO resting backstop" in seg


def test_adoption_prices_the_backstop_off_a_real_reference():
    """An adopted unknown orphan has only the broker's average price -- which is
    the correct reference, not a guess."""
    seg = SRC[SRC.index("Adopt orphan position on boot"):]
    seg = seg[:seg.index("trade_date = date.today()")]
    assert 'orphan.get("entry_price")' in seg
