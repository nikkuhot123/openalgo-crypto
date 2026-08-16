#!/usr/bin/env python
"""RECONCILE safety in pov_wall_squeeze_strategy.sync_positions_with_book.

2026-08-14, LIVE MONEY. POV opened three SENSEX legs at 12:49. The broker's
positionbook did not list them, so RECONCILE pruned all three and cancelled
their stop-losses:

    77800CE   entry REJECTED           -> pruning was correct
    78100CE   entry COMPLETE @ 333.05  -> live position, stop cancelled
    77900CE   entry COMPLETE @ 433.95  -> live position, stop cancelled

77900CE's stop sat at 420.1 while the leg traded 426-436 at the moment it was
pruned, so it was genuinely open and simply unprotected from then on. Both ran
naked into the broker's MIS auto-squareoff. Neither P&L reached the strategy's
books or its circuit breakers, and the exits are unrecoverable because the
broker only serves the current session.

Second occurrence of this failure mode -- the July one lost 75-80% on three
legs the same way, via a different trigger.

The rule these tests pin: a protective stop is NEVER cancelled on a bare
positionbook miss. Pruning requires positive evidence.
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

pov = pytest.importorskip("pov_wall_squeeze_strategy")

SYM = "SENSEX20AUG2677900CE"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    pov._recon_miss.clear()
    monkeypatch.setattr(pov, "release_symbol_lock", lambda *a, **k: None)
    monkeypatch.setattr(pov, "persist_positions", lambda *a, **k: None)
    monkeypatch.setattr(pov, "sync_direction_locks", lambda *a, **k: None)
    yield
    pov._recon_miss.clear()


def _wire(monkeypatch, *, book_qty, entry_status, sl_status):
    """positionbook reports book_qty; orderstatus answers per order id."""
    cancels = []
    monkeypatch.setattr(pov, "live_position_qty", lambda u, s: book_qty)

    def _status(order_id=None, **kw):
        st = entry_status if order_id == "ENTRY1" else sl_status
        return {"status": "success", "data": {"order_status": st}} if st else {"status": "error"}

    monkeypatch.setattr(pov.client, "orderstatus", _status, raising=False)

    def _cancel(order_id=None, **kw):
        cancels.append(order_id)
        return {"status": "success"}

    monkeypatch.setattr(pov.client, "cancelorder", _cancel, raising=False)
    return cancels


def _positions():
    return {SYM: {"qty": 20, "sl_orderid": "SL1", "entry_orderid": "ENTRY1",
                  "sl_price": 420.1, "target_price": 455.85,
                  "entry_opt_price": 433.95}}


def test_live_position_is_not_pruned_and_stop_stays_armed(monkeypatch):
    """THE 2026-08-14 BUG. Entry complete, SL live, positionbook empty."""
    cancels = _wire(monkeypatch, book_qty=0, entry_status="complete", sl_status="trigger_pending")
    pos = _positions()
    pruned = pov.sync_positions_with_book(pos, "SENSEX")
    assert pruned == 0, "a filled position must never be pruned on a book miss"
    assert SYM in pos, "position must remain tracked"
    assert cancels == [], "the protective stop must NOT be cancelled"


def test_rejected_entry_is_pruned(monkeypatch):
    """77800CE: the entry never became a position. Pruning is correct."""
    cancels = _wire(monkeypatch, book_qty=0, entry_status="rejected", sl_status="trigger_pending")
    pos = _positions()
    assert pov.sync_positions_with_book(pos, "SENSEX") == 1
    assert pos == {}
    assert cancels == ["SL1"], "the orphan stop should be cancelled"


def test_stopped_out_position_is_pruned(monkeypatch):
    """SL filled -> genuinely closed -> prune, and do not re-cancel the SL."""
    cancels = _wire(monkeypatch, book_qty=0, entry_status="complete", sl_status="complete")
    pos = _positions()
    assert pov.sync_positions_with_book(pos, "SENSEX") == 1
    assert pos == {}
    assert cancels == [], "a filled SL needs no cancellation"


def test_undetermined_state_requires_repeated_misses(monkeypatch):
    """No order state available: hold the stop until MISS_LIMIT is reached."""
    cancels = _wire(monkeypatch, book_qty=0, entry_status=None, sl_status=None)
    pos = _positions()
    for i in range(pov.RECON_MISS_LIMIT - 1):
        assert pov.sync_positions_with_book(pos, "SENSEX") == 0, f"pruned on miss {i+1}"
        assert cancels == []
    assert pov.sync_positions_with_book(pos, "SENSEX") == 1
    assert cancels == ["SL1"]


def test_miss_counter_resets_when_position_reappears(monkeypatch):
    """A transient book gap must not accumulate toward a prune."""
    _wire(monkeypatch, book_qty=0, entry_status=None, sl_status=None)
    pos = _positions()
    pov.sync_positions_with_book(pos, "SENSEX")
    assert pov._recon_miss.get(SYM) == 1
    monkeypatch.setattr(pov, "live_position_qty", lambda u, s: 20)
    pov.sync_positions_with_book(pos, "SENSEX")
    assert SYM not in pov._recon_miss
    assert SYM in pos


def test_unverifiable_book_leaves_everything_alone(monkeypatch):
    """positionbook call itself failed -> no decision, no cancellation."""
    cancels = _wire(monkeypatch, book_qty=None, entry_status="complete", sl_status="trigger_pending")
    pos = _positions()
    assert pov.sync_positions_with_book(pos, "SENSEX") == 0
    assert SYM in pos
    assert cancels == []


def test_open_position_present_in_book_is_untouched(monkeypatch):
    cancels = _wire(monkeypatch, book_qty=20, entry_status="complete", sl_status="trigger_pending")
    pos = _positions()
    assert pov.sync_positions_with_book(pos, "SENSEX") == 0
    assert SYM in pos
    assert cancels == []


def test_order_state_maps_broker_vocabulary():
    import types as _t
    cases = {"complete": "complete", "COMPLETE": "complete", "filled": "complete",
             "rejected": "rejected", "cancelled": "cancelled", "canceled": "cancelled",
             "trigger_pending": "pending", "open": "pending"}
    for raw, expected in cases.items():
        pov.client.orderstatus = (lambda r: (lambda **kw: {"status": "success",
                                                           "data": {"order_status": r}}))(raw)
        assert pov.order_state("X") == expected, raw


def test_order_state_none_without_id():
    assert pov.order_state(None) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
