#!/usr/bin/env python
"""POV's stated R:R is not its actual R:R.

sl/t1/t2/t3 are computed from the SIGNAL CANDLE CLOSE; the position fills
elsewhere and the stop is never re-derived from the fill. Measured over the first
6 live entries (recovering the signal close from t1 = e + 1.5*(e-sl)):

    contract                sig close   fill    slip     SL%   actual R at T1
    NIFTY18AUG2624300CE          9.75  10.15   +4.1%   18.2%       0.96R
    NIFTY25AUG2624150CE        105.65 106.45   +0.8%    2.4%       0.72R
    SENSEX20AUG2677500CE        75.25  71.65   -4.8%    2.7%       6.24R
    SENSEX20AUG2677700CE        31.95  27.20  -14.9%   16.9%       4.08R
    SENSEX20AUG2677600CE        59.10  60.00   +1.5%   26.1%       1.36R
    SENSEX20AUG2677500PE        64.60  54.40  -15.8%    9.7%       6.36R

The code believes every T1 is 1.50R. Same class as Judas's MIN_EFFECTIVE_RR bug,
reached via fill slippage rather than stop flooring.

Logged, NOT corrected: POV is the only positive-expectancy strategy here and it
earned that with this geometry. These tests pin the OBSERVABILITY so the
distortion cannot go unmeasured again.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "strategies" / "examples" / "pov_wall_squeeze_strategy.py").read_text(encoding="utf-8")

# (contract, sl, t1, actual_fill)
LIVE = [
    ("NIFTY18AUG2624300CE", 8.30, 11.92, 10.15),
    ("NIFTY25AUG2624150CE", 103.90, 108.28, 106.45),
    ("SENSEX20AUG2677500CE", 69.75, 83.50, 71.65),
    ("SENSEX20AUG2677700CE", 22.60, 45.97, 27.20),
    ("SENSEX20AUG2677600CE", 44.35, 81.22, 60.00),
    ("SENSEX20AUG2677500PE", 49.15, 87.77, 54.40),
]


def actual_r(sl, t1, fill):
    return (t1 - fill) / (fill - sl)


def signal_close(sl, t1):
    """Invert t1 = e + 1.5*(e - sl)."""
    return (t1 + 1.5 * sl) / 2.5


def test_geometry_is_derived_from_signal_close_not_fill():
    """If it were derived from the fill, every actual R would be exactly 1.50."""
    rs = [actual_r(sl, t1, f) for _, sl, t1, f in LIVE]
    assert not all(abs(r - 1.5) < 0.05 for r in rs)
    assert min(rs) < 1.0 < max(rs)


def test_actual_r_spans_the_measured_range():
    rs = [actual_r(sl, t1, f) for _, sl, t1, f in LIVE]
    assert min(rs) == pytest.approx(0.72, abs=0.02)
    assert max(rs) == pytest.approx(6.36, abs=0.02)


def test_two_of_six_trades_had_t1_paying_under_1r():
    rs = [actual_r(sl, t1, f) for _, sl, t1, f in LIVE]
    assert sum(1 for r in rs if r < 1.0) == 2


def test_stop_distance_is_sometimes_dangerously_tight():
    """Two entries stopped only ~2.5% from the premium. The measured live spread
    is ~0.41% of premium, so that is roughly 6 spreads of room."""
    pcts = [100 * (f - sl) / f for _, sl, t1, f in LIVE]
    assert min(pcts) < 3.0
    assert max(pcts) > 25.0


def test_fill_slippage_against_signal_close_is_material():
    slips = [100 * (f - signal_close(sl, t1)) / signal_close(sl, t1)
             for _, sl, t1, f in LIVE]
    assert min(slips) < -14.0        # -15.8% observed
    assert max(slips) > 4.0          # +4.1% observed


# ------------------------------------------------------------ observability

def test_realised_geometry_is_logged_at_entry():
    assert "GEOMETRY %s fill=" in SRC
    assert "intended 1.50R" in SRC


def test_inverted_geometry_warns():
    assert "GEOMETRY INVERTED" in SRC
    seg = SRC[SRC.index("GEOMETRY INVERTED") - 400:SRC.index("GEOMETRY INVERTED")]
    assert "_r1 < 1.0" in seg


def test_unmeasurable_risk_warns_instead_of_dividing_by_zero():
    """A stop at or above the fill must not silently produce a bogus R."""
    assert "risk is " in SRC and "not measurable" in SRC
    assert "if _rk > 0:" in SRC


def test_logging_cannot_break_the_entry():
    """Diagnostics must never affect a position that was just opened."""
    seg = SRC[SRC.index("GEOMETRY %s fill=") - 900:]
    seg = seg[:seg.index("TAPE %s quadrant")]
    assert "try:" in seg and "except Exception" in seg


def test_geometry_is_not_silently_corrected():
    """POV earned its edge WITH this geometry; changing targets is an untested
    change to the only profitable strategy. Must remain observability only."""
    assert "deliberately NOT corrected" in SRC
    # the entry geometry must still come from res[...]
    assert '"sl_price": res["sl"]' in SRC


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
