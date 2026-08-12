#!/usr/bin/env python
"""OI x price quadrant annotation in judas / pov.

Ported from openmtops narrative.py `_action_label`. Recorded at every entry as
DIAGNOSTIC context -- it must never gate a trade and must never raise, because
it runs immediately after a position has been opened.

The point of collecting it: POV's edge lives in positioning rather than price
geometry (it is the only positive-expectancy strategy here, +Rs 108/trade at
62% win, while six price-pattern strategies died between PF 0.70 and 1.00).
Tagging each fill with what the tape was doing gives the n>=15 give-back study
a covariate to explain outcomes against.
"""
import os
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategies" / "examples"))

_stub = types.ModuleType("openalgo")
_stub.api = lambda **kwargs: types.SimpleNamespace(**kwargs)
sys.modules.setdefault("openalgo", _stub)
os.environ.setdefault("OPENALGO_API_KEY", "test")

judas = pytest.importorskip("judas_swing_strategy")
pov = pytest.importorskip("pov_wall_squeeze_strategy")

MODULES = [pytest.param(judas, id="judas"), pytest.param(pov, id="pov")]


def _df(oi_from, oi_to, px_from, px_to, n=20):
    """A frame whose first and last rows carry the requested endpoints."""
    return pd.DataFrame({
        "oi": [oi_from] * (n - 1) + [oi_to],
        "close": [px_from] * (n - 1) + [px_to],
    })


@pytest.mark.parametrize("mod", MODULES)
def test_long_buildup(mod):
    lab, doi, dpx = mod.oi_price_quadrant(_df(100_000, 110_000, 100.0, 110.0))
    assert lab == "long_buildup"
    assert doi == pytest.approx(10.0)
    assert dpx == pytest.approx(10.0)


@pytest.mark.parametrize("mod", MODULES)
def test_fresh_writing(mod):
    lab, _, _ = mod.oi_price_quadrant(_df(100_000, 110_000, 100.0, 90.0))
    assert lab == "fresh_writing"


@pytest.mark.parametrize("mod", MODULES)
def test_short_covering(mod):
    """OI falling while price rises -- the squeeze POV is built to catch."""
    lab, _, _ = mod.oi_price_quadrant(_df(100_000, 90_000, 100.0, 110.0))
    assert lab == "short_covering"


@pytest.mark.parametrize("mod", MODULES)
def test_long_unwinding(mod):
    lab, _, _ = mod.oi_price_quadrant(_df(100_000, 90_000, 100.0, 90.0))
    assert lab == "long_unwinding"


@pytest.mark.parametrize("mod", MODULES)
def test_small_moves_are_unclear_not_forced(mod):
    """Below upstream's thresholds the label must be None, not a coin flip."""
    lab, doi, dpx = mod.oi_price_quadrant(_df(100_000, 100_500, 100.0, 100.5))
    assert lab is None
    assert doi == pytest.approx(0.5)
    assert dpx == pytest.approx(0.5)


@pytest.mark.parametrize("mod", MODULES)
def test_missing_oi_column_is_handled(mod):
    d = pd.DataFrame({"close": [100.0] * 20})
    assert mod.oi_price_quadrant(d) == (None, 0.0, 0.0)


@pytest.mark.parametrize("mod", MODULES)
def test_short_frame_is_handled(mod):
    assert mod.oi_price_quadrant(_df(1, 2, 1.0, 2.0, n=3)) == (None, 0.0, 0.0)


@pytest.mark.parametrize("mod", MODULES)
def test_none_frame_is_handled(mod):
    assert mod.oi_price_quadrant(None) == (None, 0.0, 0.0)


@pytest.mark.parametrize("mod", MODULES)
def test_zero_baseline_does_not_divide_by_zero(mod):
    assert mod.oi_price_quadrant(_df(0, 5_000, 0.0, 10.0)) == (None, 0.0, 0.0)


@pytest.mark.parametrize("mod", MODULES)
def test_never_raises_on_garbage(mod):
    """It runs right after a fill -- an exception here must not reach the
    position-management path."""
    for junk in ("not a frame", 42, pd.DataFrame()):
        assert mod.oi_price_quadrant(junk) == (None, 0.0, 0.0)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
