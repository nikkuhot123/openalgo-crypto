#!/usr/bin/env python
"""Lot-size detection in judas_swing_strategy / pov_wall_squeeze_strategy.

2026-08-12: every order both strategies placed was rejected --
    "Quantity must be in multiples of lot size 65"   (NIFTY, needs 65)
    "Quantity must be in multiples of lot size 20"   (SENSEX, needs 20)
51 rejections across the two books, nothing reached sandbox_orders.

Cause: fetch_lot_size() had ONE source, client.optionchain(), and that endpoint
returned 404 "No strikes found for NIFTY expiring 18-AUG-26 ... update master
contract" all session -- on both indices -- even though the symbol master held
462 CE rows for that exact expiry. The failure was intermittent: the same call
succeeded again two hours later. On failure the code fell through to

    QUANTITY = 75   # fallback

75 is NIFTY's lot size from BEFORE the 2025-12-31 change to 65, and was never
correct for SENSEX. Analyzer rejected it outright, which is the benign case;
live would have risked a wrong-sized REAL position.

These tests pin the two properties that matter:
  1. a second, independent source exists and is used when the first fails
  2. when BOTH fail the code refuses to guess
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

judas = pytest.importorskip("judas_swing_strategy")
pov = pytest.importorskip("pov_wall_squeeze_strategy")

MODULES = [pytest.param(judas, id="judas"), pytest.param(pov, id="pov")]

# the real shapes, captured from the live API on 2026-08-12
CHAIN_OK = {"status": "success", "chain": [{"ce": {"lotsize": 65}, "pe": {"lotsize": 65}}]}
CHAIN_404 = {"status": "error",
             "message": 'HTTP 404: {"message": "No strikes found for NIFTY expiring '
                        '18-AUG-26. Please check expiry date or update master contract."}'}
OPTSYM_OK = {"status": "success", "symbol": "NIFTY18AUG2624450CE", "lotsize": 65,
             "tick_size": 0.05, "freeze_qty": 1800, "underlying_ltp": 24435.95}
OPTSYM_FAIL = {"status": "error", "message": "boom"}


def _wire(monkeypatch, mod, chain, optsym, expiry="18AUG26"):
    monkeypatch.setattr(mod, "get_nearest_expiry", lambda *a, **k: expiry)
    calls = {"chain": 0, "optsym": 0}

    def _chain(**kw):
        calls["chain"] += 1
        if isinstance(chain, Exception):
            raise chain
        return chain

    def _optsym(**kw):
        calls["optsym"] += 1
        if isinstance(optsym, Exception):
            raise optsym
        return optsym

    monkeypatch.setattr(mod.client, "optionchain", _chain, raising=False)
    monkeypatch.setattr(mod.client, "optionsymbol", _optsym, raising=False)
    return calls


@pytest.mark.parametrize("mod", MODULES)
def test_source1_optionchain_used_when_healthy(monkeypatch, mod):
    calls = _wire(monkeypatch, mod, CHAIN_OK, OPTSYM_OK)
    assert mod.fetch_lot_size("NIFTY", "NSE_INDEX", "NFO") == 65
    assert calls["chain"] == 1
    assert calls["optsym"] == 0, "must not make a second call when the first works"


@pytest.mark.parametrize("mod", MODULES)
def test_falls_back_when_optionchain_404s(monkeypatch, mod):
    """The exact 2026-08-12 failure: optionchain 404s, detection must survive."""
    calls = _wire(monkeypatch, mod, CHAIN_404, OPTSYM_OK)
    assert mod.fetch_lot_size("NIFTY", "NSE_INDEX", "NFO") == 65
    assert calls["optsym"] == 1


@pytest.mark.parametrize("mod", MODULES)
def test_falls_back_when_optionchain_raises(monkeypatch, mod):
    _wire(monkeypatch, mod, RuntimeError("connection reset"), OPTSYM_OK)
    assert mod.fetch_lot_size("NIFTY", "NSE_INDEX", "NFO") == 65


@pytest.mark.parametrize("mod", MODULES)
def test_sensex_lot_size_is_not_niftys(monkeypatch, mod):
    """20, never 65 and never the old 75. SENSEX was rejected 32 times."""
    sensex = dict(OPTSYM_OK, symbol="SENSEX13AUG2678100CE", lotsize=20)
    _wire(monkeypatch, mod, CHAIN_404, sensex, expiry="13AUG26")
    assert mod.fetch_lot_size("SENSEX", "BSE_INDEX", "BFO") == 20


@pytest.mark.parametrize("mod", MODULES)
def test_returns_none_when_both_sources_fail(monkeypatch, mod):
    """Must NOT invent a number. None is what makes the caller stand down."""
    _wire(monkeypatch, mod, CHAIN_404, OPTSYM_FAIL)
    assert mod.fetch_lot_size("NIFTY", "NSE_INDEX", "NFO") is None


@pytest.mark.parametrize("mod", MODULES)
def test_returns_none_when_expiry_unavailable(monkeypatch, mod):
    _wire(monkeypatch, mod, CHAIN_OK, OPTSYM_OK, expiry=None)
    assert mod.fetch_lot_size("NIFTY", "NSE_INDEX", "NFO") is None


@pytest.mark.parametrize("mod", MODULES)
def test_no_stale_75_fallback_remains_in_source(mod):
    """The literal that caused it must be gone from the sizing path."""
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "QUANTITY = 75" not in src
    assert "LOT_SIZE = 75" not in src
    assert "using default" not in src


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
