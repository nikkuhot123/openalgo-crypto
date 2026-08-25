#!/usr/bin/env python
"""Renko's exchange-resting crash backstop.

2026-08-25: a Flattrade rate-limit storm (133 req/min against a 120 cap) starved
the single eventlet worker. Renko was holding two live MIS positions
(SENSEX27AUG2677400CE x20, MIDCPNIFTY25AUG2614875CE x120). Its stop is a SPOT
level checked in-process every poll, so with no API there was no protection and
no way to exit -- both had to be closed by hand from the broker terminal.

POV and Judas both survive that class of outage because their protection RESTS
at the exchange. This suite pins the same property for renko.

The level is deliberately wide: translating a spot stop into a premium is
unreliable (median 14.3% mis-placement, which is why Judas rejected it), while
the worst adverse premium excursion measured across 8 contracts was -48.6%. A
-60% floor therefore cannot pre-empt the primary spot stop.
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[1] / "strategies" / "examples"
       / "renko_engine_strategy.py").read_text(encoding="utf-8")


def test_a_resting_stop_is_placed_at_all():
    """The whole point: renko used to place ZERO protective orders."""
    assert "def place_disaster_stop" in SRC
    assert "place_disaster_stop(symbol, fill_prem, qty_total)" in SRC, \
        "backstop is never armed at entry"


def test_stop_is_SL_not_SL_M():
    """SL-M was rejected 33/33 for options and the API reported those
    rejections as success with orderid=null -- silently unprotected."""
    blk = SRC[SRC.index("def place_disaster_stop"):][:2600]
    assert 'price_type="SL"' in blk
    assert "SL-M" not in blk.replace("never \"SL-M\"", "").replace("SL-M was", "")


def test_limit_sits_below_trigger():
    blk = SRC[SRC.index("def place_disaster_stop"):][:2600]
    assert "SL_LIMIT_BUFFER_PCT" in blk, "a stop-limit needs room to fill in a gap"
    assert "trigger_price=trg" in blk and "price=lim" in blk


def test_default_is_60_percent():
    m = re.search(r"DISASTER_STOP_PCT = float\(os\.getenv\('DISASTER_STOP_PCT', '(\d+)'\)\)", SRC)
    assert m, "constant missing"
    assert int(m.group(1)) == 60, "must not pre-empt the primary spot stop"


def test_tiny_premium_is_skipped_not_armed_at_zero():
    """A 0.10 premium gives a 0.04 trigger, which tick-rounds to 0.05 and would
    arm a meaningless sell-at-almost-zero. Judas hit this exact edge."""
    blk = SRC[SRC.index("def place_disaster_stop"):][:2600]
    assert "raw < 0.10" in blk, "no guard on the RAW pre-rounded value"
    assert blk.index("raw < 0.10") < blk.index("trg = round("), \
        "guard must run BEFORE tick rounding"


def test_order_id_is_persisted():
    """A restart cannot cancel a resting order it does not know about."""
    assert '"disaster_oid": d_oid' in SRC
    i = SRC.index('"disaster_oid": d_oid')
    assert "persist(pos)" in SRC[i:i + 400]


def test_every_sell_path_cancels_the_backstop_first():
    """The broker reserves the position quantity against a resting stop, so a
    SELL sent while it rests is REJECTED -- the exact reason the UI "Close
    Position" button failed on 2026-08-24. All three exit paths must cancel."""
    guards = SRC.count("disaster stop cancel")
    sells = len(re.findall(r'action="SELL"', SRC))
    # one SELL is the backstop placement itself, which must NOT carry a guard
    assert guards == sells - 1, f"{guards} guards for {sells - 1} exit SELLs"
    assert guards == 3, "expected target, EOD and shutdown exits"


def test_no_guard_inside_the_placement_function():
    """A guard there would reference `pos`, which is not in scope -- NameError."""
    blk = SRC[SRC.index("def place_disaster_stop"):SRC.index("def confirm_entry_fill")]
    assert "disaster stop cancel" not in blk


def test_cancel_treats_already_terminal_as_success():
    """Cancelling a filled or rejected order errors, but that IS the goal."""
    blk = SRC[SRC.index("def safe_cancel_order"):][:1400]
    for token in ("complet", "reject", "cancel"):
        assert token in blk, f"terminal state '{token}' not mapped to success"


def test_failure_to_arm_is_loud():
    """Silence is how a position ends up unprotected while the log looks fine."""
    blk = SRC[SRC.index("def place_disaster_stop"):][:2600]
    assert "DISASTER STOP NOT ARMED" in blk
    assert "log.warning" in blk or "log.error" in blk


def test_armed_even_for_an_unconfirmed_entry():
    """The 14:49 entry returned 'Request timed out' and was tracked as live. If
    that order did fill, the resting stop is the only surviving protection."""
    i = SRC.index("unconfirmed -- tracking as live")
    assert "place_disaster_stop" in SRC[i:i + 900], \
        "unknown-fill path must still arm the backstop"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
