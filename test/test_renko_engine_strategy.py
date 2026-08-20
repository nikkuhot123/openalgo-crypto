#!/usr/bin/env python
"""Tests for renko_engine_strategy -- the SENSEX / MIDCPNIFTY forward test.

The properties that matter here are safety properties, because this strategy is
being deployed on evidence that is deliberately incomplete:

  - SENSEX is profitable in 2 of 7 months on real premiums and March 2026 alone
    carries the whole result.
  - MIDCPNIFTY has NO real-premium verification (unsupported on Volrix, no
    weekly options, corrected model edge 1.94x its hurdle).

So the tests assert that it CANNOT quietly go live, cannot invent a lot size,
and cannot share state with a live sibling.
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


def load(**env):
    """Import the strategy fresh with a given environment."""
    for k in ("UNDERLYING", "STRATEGY_NAME", "DRY_RUN", "QUANTITY", "MAX_LOTS"):
        os.environ.pop(k, None)
    os.environ["OPENALGO_API_KEY"] = "test"
    os.environ.update({k: str(v) for k, v in env.items()})
    sys.modules.pop("renko_engine_strategy", None)
    return importlib.import_module("renko_engine_strategy")


# ------------------------------------------------------- safety: shadow default

def test_shadow_is_the_default():
    """Inverted from the other strategies ON PURPOSE. Neither symbol has
    evidence good enough for capital, so live must be opt-IN."""
    m = load(UNDERLYING="SENSEX")
    assert m.DRY_RUN is True


def test_shadow_name_forces_shadow_even_if_env_says_live():
    """The UI upload form writes only name/exchange/schedule, so the name is the
    one channel that survives. A registration called "... SHADOW" must be shadow
    regardless of what DRY_RUN says."""
    m = load(UNDERLYING="SENSEX", DRY_RUN="false", STRATEGY_NAME="Renko Engine (SENSEX) SHADOW")
    assert m.DRY_RUN is True


def test_live_requires_explicit_optin():
    m = load(UNDERLYING="SENSEX", DRY_RUN="false", STRATEGY_NAME="Renko Engine (SENSEX)")
    assert m.DRY_RUN is False


def test_shadow_state_file_is_isolated():
    """A live sibling adopts whatever the snapshot describes on restart, so a
    simulated position must never land in the live file."""
    shadow = load(UNDERLYING="SENSEX")
    live = load(UNDERLYING="SENSEX", DRY_RUN="false", STRATEGY_NAME="Renko Engine (SENSEX)")
    assert "_shadow" in shadow.STATE_FILE.name
    assert "_shadow" not in live.STATE_FILE.name
    assert shadow.STATE_FILE != live.STATE_FILE


# ------------------------------------------------------------ config integrity

def test_config_matches_the_swept_winner():
    """These are the values the 12,096-config sweep selected on net rupees after
    friction. Changing them invalidates the evidence in the docstring."""
    m = load(UNDERLYING="SENSEX")
    assert m.T1_RR == 2.5
    assert m.T2_RR == 3.0
    assert m.MAX_TRADES_DAY == 2
    assert m.MIN_ROOM_R == 2.0
    assert m.BRICK_PCT == 0.66
    assert m.TIMEFRAME_MIN == 15
    assert m.EMA_SLOW_LEN == 30
    assert m.LEVEL_TOL == 8.0


def test_t2_is_beyond_t1():
    """The sweep's nominal 2.0R resolves to 3.0R because T2 is held strictly
    beyond T1; a config with T2 inside T1 would exit backwards."""
    m = load(UNDERLYING="SENSEX")
    assert m.T2_RR > m.T1_RR


@pytest.mark.parametrize("sym,idx,opt", [
    ("SENSEX", "BSE_INDEX", "BFO"),
    ("MIDCPNIFTY", "NSE_INDEX", "NFO"),
])
def test_exchange_routing(sym, idx, opt):
    m = load(UNDERLYING=sym)
    assert m.IDX_EXCHANGE == idx
    assert m.OPT_EXCHANGE == opt


def test_unsupported_underlying_exits():
    with pytest.raises(SystemExit):
        load(UNDERLYING="RELIANCE")


# ----------------------------------------------------------------- mechanics

def test_renko_steps_in_whole_bricks_and_never_rewrites():
    """The brick is recomputed from the LIVE close every call, matching the PRO
    panel (its NIFTY frame read 161.93 against a 24,534.45 close, i.e. tracking
    price rather than frozen at the open). So a step is a whole multiple of the
    CURRENT brick, not the brick in force when the base was last set."""
    m = load(UNDERLYING="SENSEX")
    r = m.Renko()
    _lo, _hi, brick0 = r.update(78000.0)
    assert r.base == 78000.0
    assert brick0 == pytest.approx(78000.0 * 0.0066)
    base0 = r.base

    r.update(78000.0 + brick0 * 0.5)          # inside the brick -> no step
    assert r.base == base0

    probe = 78000.0 + brick0 * 2.2
    brick_now = probe * 0.0066                # brick tracks price
    r.update(probe)
    steps = int((probe - base0) / brick_now)
    assert steps == 2
    assert r.base == pytest.approx(base0 + steps * brick_now, rel=1e-9)
    # and history is never rewritten: the base only ever moves in whole bricks
    assert (r.base - base0) % brick_now == pytest.approx(0.0, abs=1e-6)


def test_renko_brick_tracks_price():
    m = load(UNDERLYING="SENSEX")
    r = m.Renko()
    _, _, small = r.update(12000.0)
    _, _, big = m.Renko().update(78000.0)
    assert big > small
    assert big / small == pytest.approx(78000.0 / 12000.0, rel=1e-6)


def test_renko_boundaries_straddle_base():
    m = load(UNDERLYING="SENSEX")
    r = m.Renko()
    lo, hi, brick = r.update(12000.0)
    assert lo < r.base < hi
    assert hi - lo == pytest.approx(2 * brick)


def test_touches_uses_the_tolerance_band():
    m = load(UNDERLYING="SENSEX")
    assert m.touches(100.0, 105.0, 110.0) is True      # 5 below lo, within 8
    assert m.touches(100.0, 109.0, 110.0) is False     # 9 below lo, outside 8
    assert m.touches(None, 1.0, 2.0) is False


def test_statutory_cost_charges_both_sides():
    m = load(UNDERLYING="SENSEX")
    c = m.statutory_cost(100.0, 120.0, 20)
    assert c == pytest.approx((100.0 + 120.0) * 20 * 0.12 / 100.0)
    assert m.statutory_cost(None, 1.0, 20) == 0.0


def test_hhmm():
    m = load(UNDERLYING="SENSEX")
    assert m.hhmm("15:15") == 915
    assert m.hhmm("09:30") == 570


def test_prior_day_levels_cpr_math():
    import pandas as pd
    m = load(UNDERLYING="SENSEX")
    idx = pd.to_datetime(["2026-08-18 09:15", "2026-08-18 15:15",
                          "2026-08-19 09:15", "2026-08-19 09:30"]).tz_localize("Asia/Kolkata")
    df = pd.DataFrame({"open": [1, 1, 1, 1], "high": [110.0, 120.0, 5, 5],
                       "low": [90.0, 80.0, 1, 1], "close": [100.0, 100.0, 2, 2]}, index=idx)
    d = m.prior_day_levels(df)
    pdh, pdl, pdc = 120.0, 80.0, 100.0
    assert d["pdh"] == pdh and d["pdl"] == pdl and d["pdc"] == pdc
    assert d["cpp"] == pytest.approx((pdh + pdl + pdc) / 3.0)
    assert d["cpr_hi"] >= d["cpr_lo"]
    # institutional zone comes from the PRIOR session's closing bars
    assert d["inst_hi"] == 120.0


def test_prior_day_levels_needs_two_sessions():
    import pandas as pd
    m = load(UNDERLYING="SENSEX")
    idx = pd.to_datetime(["2026-08-19 09:15"]).tz_localize("Asia/Kolkata")
    df = pd.DataFrame({"open": [1], "high": [2.0], "low": [1.0], "close": [1.5]}, index=idx)
    assert m.prior_day_levels(df) == {}


# ------------------------------------------------- fail closed, never guess

def test_lot_size_returns_none_when_both_sources_fail():
    """2026-08-12: optionchain 404'd all session, a hardcoded 75 was used, and
    every order was rejected. Both sources failing must yield None."""
    m = load(UNDERLYING="MIDCPNIFTY")

    class Dead:
        def expiry(self, **k):
            return {"status": "success", "data": ["25-AUG-26"]}

        def optionchain(self, **k):
            return {"status": "error", "message": "404 no strikes"}

        def optionsymbol(self, **k):
            return {"status": "error"}

    m.client = Dead()
    assert m.fetch_lot_size() is None


def test_lot_size_second_source_rescues_a_dead_optionchain():
    m = load(UNDERLYING="MIDCPNIFTY")

    class Half:
        def expiry(self, **k):
            return {"status": "success", "data": ["25-AUG-26"]}

        def optionchain(self, **k):
            raise RuntimeError("boom")

        def optionsymbol(self, **k):
            return {"status": "success", "symbol": "X", "lotsize": 120}

    m.client = Half()
    assert m.fetch_lot_size() == 120


def test_no_hardcoded_lot_fallback_in_source():
    src = (ROOT / "strategies" / "examples" / "renko_engine_strategy.py").read_text(encoding="utf-8")
    assert "= 75" not in src
    assert "lot = 75" not in src
    # standing down must be the documented behaviour
    assert "standing down rather than" in src


def test_option_ltp_rejects_a_spot_leak():
    m = load(UNDERLYING="SENSEX")

    class Leak:
        def quotes(self, **k):
            return {"status": "success", "data": {"ltp": 78000.0}}

    m.client = Leak()
    assert m.fetch_option_ltp("SENSEX20AUG2678000CE", spot=78000.0) is None


# --------------------------------------------------------- intraday only
# User requirement 2026-08-20: intraday only. The evidence base is intraday --
# every backtest exits at the session close -- so an overnight carry would be an
# untested strategy wearing a tested one's numbers.

def test_product_is_mis_and_not_configurable():
    m = load(UNDERLYING="SENSEX")
    assert m.PRODUCT == "MIS"
    src = (ROOT / "strategies" / "examples" / "renko_engine_strategy.py").read_text(encoding="utf-8")
    # must NOT be reachable from the environment -- MIS is the guarantee
    assert "getenv('PRODUCT'" not in src and 'getenv("PRODUCT"' not in src


def test_no_nrml_order_path_exists():
    src = (ROOT / "strategies" / "examples" / "renko_engine_strategy.py").read_text(encoding="utf-8")
    code_lines = [ln for ln in src.splitlines() if not ln.strip().startswith("#")]
    assert not any("NRML" in ln for ln in code_lines)
    # every order must go out on the pinned product
    assert 'product="MIS"' not in src
    assert src.count("product=PRODUCT") >= 3


def test_eod_squareoff_is_not_gated_behind_a_new_candle():
    """The first version checked EOD inside the once-per-bar block, so a stalled
    feed after 15:15 meant no bar, no check, and an overnight carry. The hard
    exit must sit ABOVE the per-bar gate."""
    src = (ROOT / "strategies" / "examples" / "renko_engine_strategy.py").read_text(encoding="utf-8")
    hard = src.index("HARD intraday square-off")
    gate = src.index("# act once per completed bar")
    assert hard < gate, "EOD square-off must precede the per-bar gate"
    # and it must be driven by the clock, not by bar arrival
    seg = src[hard:gate]
    assert "mins >= eod" in seg


def test_entry_window_closes_before_eod():
    m = load(UNDERLYING="SENSEX")
    assert m.hhmm(m.ENTRY_END) < m.hhmm(m.EOD_EXIT)


def test_shutdown_closes_a_live_position():
    src = (ROOT / "strategies" / "examples" / "renko_engine_strategy.py").read_text(encoding="utf-8")
    tail = src[src.index("if pos is not None and not DRY_RUN:"):]
    assert "action=\"SELL\"" in tail and "product=PRODUCT" in tail

def test_preopen_artefact_cannot_become_the_x_candle():
    """2026-08-20, live: the 1m feed carried a flat 09:00 SENSEX candle
    (o=h=l=c=77468.45). It resampled into its own 15m bucket and became bar[0]
    of the session, so x_high == x_low, x_44 == x_56, and both the X-band zone
    block and the `close > x_56` filter were silently neutered."""
    import pandas as pd
    m = load(UNDERLYING="SENSEX")
    idx = pd.to_datetime([
        "2026-08-20 09:00",                       # pre-open artefact
        "2026-08-20 09:15", "2026-08-20 09:20", "2026-08-20 09:29",
        "2026-08-20 09:30", "2026-08-20 15:29",
        "2026-08-20 15:45",                       # post-close artefact
    ]).tz_localize("Asia/Kolkata")
    raw = pd.DataFrame({
        "open":  [77468.45, 77467.0, 77480.0, 77390.0, 77405.0, 77500.0, 77510.0],
        "high":  [77468.45, 77494.8, 77490.0, 77400.0, 77489.0, 77505.0, 77515.0],
        "low":   [77468.45, 77375.8, 77470.0, 77380.0, 77390.0, 77495.0, 77505.0],
        "close": [77468.45, 77399.9, 77475.0, 77399.9, 77480.0, 77500.0, 77512.0],
    }, index=idx)

    class Feed:
        def history(self, **k):
            return raw

    m.client = Feed()
    out = m.fetch_15m()
    first = out.index[0]
    assert (first.hour, first.minute) == (9, 15), f"first bar is {first}"
    # the X candle must have a real range, not the flat artefact
    assert out.iloc[0]["high"] > out.iloc[0]["low"]
    assert out.iloc[0]["high"] == 77494.8
    assert out.iloc[0]["low"] == 77375.8
    # nothing after 15:30 survives
    mm = out.index.hour * 60 + out.index.minute
    assert mm.max() < 930

def test_option_ltp_accepts_a_real_premium():
    m = load(UNDERLYING="SENSEX")

    class Ok:
        def quotes(self, **k):
            return {"status": "success", "data": {"ltp": 240.0}}

    m.client = Ok()
    assert m.fetch_option_ltp("SENSEX20AUG2678000CE", spot=78000.0) == 240.0


def test_shadow_csv_header_supports_the_pass_condition():
    """The pre-registered condition is 'profitable in a MAJORITY of forward
    months', so the log must carry a date and a net per round trip."""
    src = (ROOT / "strategies" / "examples" / "renko_engine_strategy.py").read_text(encoding="utf-8")
    assert "date,underlying,side,symbol,qty" in src
    assert "net" in src and "append_shadow" in src


def test_sigterm_handler_registered():
    src = (ROOT / "strategies" / "examples" / "renko_engine_strategy.py").read_text(encoding="utf-8")
    assert "signal.signal(signal.SIGTERM" in src
    assert "_shutdown" in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
