#!/usr/bin/env python
"""The master-contract startup race that killed 4 live instances.

2026-08-24, a Monday. The flattrade master contract finished downloading at
09:20:50. Everything that started before that got HTTP 500 from optionchain AND
optionsymbol, so lot-size detection returned nothing:

    Renko  (09:16:00) -> 500 at 09:16:02 -> sys.exit(1) with NO retry at all
    PDH    (09:10:00) -> retried 600s from process start -> gave up 09:20:09,
                         41 SECONDS before the master was ready

POV (09:30), Judas (09:45) and the collector (09:20) started after the master
was ready and were unaffected. Standing down instead of guessing a size stays
correct -- 2026-08-12 proved a hardcoded guess gets every order rejected. The
defect is the DEADLINE: it was anchored to process start, not to the moment the
size is actually needed.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "strategies" / "examples"
RENKO = (SRC / "renko_engine_strategy.py").read_text(encoding="utf-8")
PDH = (SRC / "prior_levels_ema_strategy.py").read_text(encoding="utf-8")


def _block(src, anchor, span=1800):
    i = src.index(anchor)
    return src[i:i + span]


def _joined(src, anchor, span=1800):
    """Same slice with adjacent string literals concatenated, so a message
    wrapped across source lines is searchable as the text the user sees."""
    return re.sub(r'"\s*\n\s*"', "", _block(src, anchor, span))


# --------------------------------------------------------------- renko
def test_renko_no_longer_dies_on_the_first_failed_lookup():
    """The old code was `if not lot: sys.exit(1)` with nothing in between."""
    b = _block(RENKO, "lot = QUANTITY or fetch_lot_size()")
    assert "while not _shutdown" in b, "renko still has no retry loop"
    # the exit must sit behind the entry-cutoff test, not the first failure
    assert b.index("ENTRY_END") < b.index("sys.exit(1)"), \
        "renko exits before consulting the entry cutoff"


def test_renko_retries_until_the_entry_cutoff_not_a_fixed_window():
    b = _block(RENKO, "lot = QUANTITY or fetch_lot_size()")
    assert "hhmm(ENTRY_END)" in b, "deadline is not the entry window"
    assert not re.search(r"_waited\s*<\s*\d+", b), \
        "a fixed elapsed-seconds budget reintroduces the 41-second miss"


def test_renko_wait_is_interruptible_and_refetches():
    b = _block(RENKO, "lot = QUANTITY or fetch_lot_size()")
    assert "nap(10)" in b, "must sleep in slices so SIGTERM lands promptly"
    assert b.count("fetch_lot_size()") >= 2, "loop never re-queries the master"
    assert "sys.exit(0)" in b, "shutdown during the wait must exit cleanly"


def test_renko_still_refuses_to_guess_a_size():
    """The 2026-08-12 guard must survive: never invent a lot size."""
    b = _block(RENKO, "lot = QUANTITY or fetch_lot_size()")
    assert not re.search(r"lot\s*=\s*(75|65|20|120)\b", b), "hardcoded guess"
    assert "QUANTITY to override" in _joined(
        RENKO, "lot = QUANTITY or fetch_lot_size()")


# ----------------------------------------------------------------- pdh
def test_pdh_deadline_is_the_entry_time_not_process_start():
    b = _block(PDH, "_deadline = datetime.combine")
    assert "ENTRY_TIME" in b, "overnight deadline must be the 15:05 entry"
    assert "MODE ==" in b, "intraday and overnight need different deadlines"


def test_pdh_dropped_the_fixed_600s_budget():
    """The exact shape that expired 41s early."""
    assert "LOT_SIZE_WAIT_SECS" not in _block(PDH, "while LOT_SIZE <= 0"), \
        "fixed-seconds budget still gates the wait"
    assert not re.search(r"_waited\s*<\s*LOT_SIZE_WAIT_SECS", PDH)


def test_pdh_wait_is_interruptible():
    b = _block(PDH, "while LOT_SIZE <= 0")
    assert "_shutdown_requested" in b
    assert "sys.exit(0)" in b, "shutdown during the wait must exit cleanly"


def test_pdh_error_names_the_deadline_it_missed():
    b = _block(PDH, "while LOT_SIZE <= 0")
    assert "_deadline.strftime" in b, \
        "the failure log must say WHEN it gave up, or this recurs silently"


# --------------------------------------------------- shared invariant
def test_neither_strategy_can_exit_before_its_size_is_needed():
    """Both processes must outlive the master-contract download window."""
    for name, blk in (("renko", _block(RENKO, "lot = QUANTITY or fetch_lot_size()")),
                      ("pdh", _block(PDH, "while LOT_SIZE <= 0"))):
        loop = blk.index("while")
        exits = [m.start() for m in re.finditer(r"sys\.exit\(1\)", blk)]
        assert all(e > loop for e in exits), \
            f"{name} has a fatal exit before the wait loop"


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
