#!/usr/bin/env python
"""Greeks parsing in strategies/scripts/cas_window_logger.py.

The collector ran all of 2026-08-06 with CAS_LOG_GREEKS=true and produced
682 ATM option rows, every one with delta/theta/gamma/vega/iv EMPTY. The
endpoint returns those fields at the top level of the response, while the
code read r["data"], which the payload does not contain.

The fixture below is the verbatim shape returned by
POST /api/v1/optiongreeks on 2026-08-06 for NIFTY11AUG2624650CE.
"""
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "strategies" / "scripts"))

_stub = types.ModuleType("openalgo")
_stub.api = lambda **kwargs: types.SimpleNamespace(**kwargs)
sys.modules.setdefault("openalgo", _stub)
os.environ.setdefault("OPENALGO_API_KEY", "test")

cas = pytest.importorskip("cas_window_logger")

LIVE = {
    "days_to_expiry": 5.1299,
    "exchange": "NFO",
    "expiry_date": "11-Aug-2026",
    "greeks": {"delta": 0.5187, "gamma": 0.001157, "rho": -0.020295,
               "theta": -13.3828, "vega": 11.652},
    "implied_volatility": 11.78,
    "interest_rate": 0,
    "option_price": 144.4,
    "option_type": "CE",
    "spot_price": 24663.75,
    "status": "success",
    "strike": 24650.0,
    "symbol": "NIFTY11AUG2624650CE",
    "underlying": "NIFTY",
}


def _with(resp, monkeypatch):
    monkeypatch.setattr(cas, "client",
                        types.SimpleNamespace(optiongreeks=lambda **kw: resp))


def test_parses_the_live_top_level_shape(monkeypatch):
    _with(LIVE, monkeypatch)
    g = cas.fetch_greeks("NIFTY11AUG2624650CE", "NFO", "NIFTY", "NSE_INDEX")
    assert g["theta"] == -13.3828, "theta must survive -- this is the whole point"
    assert g["delta"] == 0.5187
    assert g["gamma"] == 0.001157
    assert g["vega"] == 11.652
    assert g["iv"] == 11.78, "iv lives at the top level as implied_volatility"


def test_still_parses_a_data_wrapped_payload(monkeypatch):
    """Fallback for a build that nests the payload under 'data'."""
    _with({"status": "success", "data": LIVE}, monkeypatch)
    g = cas.fetch_greeks("X", "NFO", "NIFTY", "NSE_INDEX")
    assert g["theta"] == -13.3828 and g["iv"] == 11.78


def test_error_status_yields_nothing(monkeypatch):
    _with({"status": "error", "message": "Symbol not found"}, monkeypatch)
    assert cas.fetch_greeks("X", "NFO", "NIFTY", "NSE_INDEX") == {}


def test_never_raises_when_the_client_blows_up(monkeypatch):
    def boom(**kw):
        raise RuntimeError("broker down")
    monkeypatch.setattr(cas, "client", types.SimpleNamespace(optiongreeks=boom))
    assert cas.fetch_greeks("X", "NFO", "NIFTY", "NSE_INDEX") == {}


def test_missing_greeks_block_degrades_to_nones(monkeypatch):
    _with({"status": "success", "implied_volatility": 9.5}, monkeypatch)
    g = cas.fetch_greeks("X", "NFO", "NIFTY", "NSE_INDEX")
    assert g["theta"] is None and g["iv"] == 9.5
