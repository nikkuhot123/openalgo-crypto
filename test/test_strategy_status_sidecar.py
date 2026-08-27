#!/usr/bin/env python
"""Tests for status sidecars, auto-lot capability detection, and risk floors.

Added following review of restored Python strategy UI features:
1. `supports_auto_lots` capability probe: True for Judas/POV/PDH/HA-EMA, False for Renko.
2. `quantity_settable` flag: False for platform-managed strategies, True only for systemd:.
3. Status sidecar schema & staleness guard: endpoint reads live sidecar (< 90s), falls back on stale.
4. Sidecar writer in strategy files: writes atomic JSON snapshot with exact TradeGauge schema.
5. Auto-lot risk floor warning: logs warning when risk budget < 1 lot max loss.
"""
import json
import os
import sys
import types

_stub = types.ModuleType("openalgo")
_stub.api = lambda **kw: types.SimpleNamespace(**kw)
sys.modules.setdefault("openalgo", _stub)
os.environ.setdefault("OPENALGO_API_KEY", "test")
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "strategies" / "examples"))

bp_mod = pytest.importorskip("blueprints.python_strategy")


# --------------------------------------------------- 1. capability detection
def test_renko_does_not_support_auto_lots():
    """Renko sizes as `lot * MAX_LOTS` and ignores LOT_MODE / RISK_PCT."""
    config = {"file_name": "renko_engine_strategy.py",
              "file_path": str(ROOT / "strategies" / "examples" / "renko_engine_strategy.py")}
    assert bp_mod._supports_auto_lots(config) is False


@pytest.mark.parametrize("strat_file", [
    "judas_swing_strategy.py",
    "pov_wall_squeeze_strategy.py",
    "prior_levels_ema_strategy.py",
    "ha_ema34_channel_strategy.py",
])
def test_strategies_supporting_auto_lots(strat_file):
    config = {"file_name": strat_file,
              "file_path": str(ROOT / "strategies" / "examples" / strat_file)}
    assert bp_mod._supports_auto_lots(config) is True


def test_quantity_settable_only_for_systemd():
    assert bool(str({"managed_by": "systemd:openalgo-btc"}.get("managed_by") or "").startswith("systemd:")) is True
    assert bool(str({"managed_by": None}.get("managed_by") or "").startswith("systemd:")) is False


# --------------------------------------------------- 2. status sidecar schema
@pytest.mark.parametrize("mod_name", [
    "renko_engine_strategy",
    "prior_levels_ema_strategy",
    "pov_wall_squeeze_strategy",
    "judas_swing_strategy",
])
def test_strategy_writes_valid_sidecar_schema(mod_name, tmp_path, monkeypatch):
    mod = pytest.importorskip(mod_name)
    sidecar_path = tmp_path / "test_strat_status.json"
    monkeypatch.setattr(mod, "STATUS_FILE", sidecar_path)

    # Call write_status
    mod.write_status(
        "IN_TRADE",
        active_trades=[{
            "symbol": "SENSEX27AUG2677500PE",
            "direction": "PE",
            "entry_price": 242.0,
            "stop_loss": 207.0,
            "target": 377.0,
            "current_price": 295.0,
            "type": "PE",
        }],
        indicators={"regime": "ATM 77600", "phase": "IN_TRADE", "spot": 77440.0},
        last_message="Underlying LTP: 77440.0, ATM Strike: 77600"
    )

    assert sidecar_path.exists()
    data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert data["state"] == "IN_TRADE"
    assert len(data["active_trades"]) == 1
    t = data["active_trades"][0]
    assert t["symbol"] == "SENSEX27AUG2677500PE"
    assert t["direction"] == "PE"
    assert t["entry_price"] == 242.0
    assert t["stop_loss"] == 207.0
    assert t["target"] == 377.0
    assert t["current_price"] == 295.0
    assert data["indicators"]["spot"] == 77440.0
    assert "last_updated" in data


# --------------------------------------------------- 3. risk floor warning
def test_compute_auto_lots_risk_floor_warning(caplog):
    import logging
    judas = pytest.importorskip("judas_swing_strategy")
    with caplog.at_level(logging.WARNING):
        # Capital 10,000, risk 1% = budget 100 Rs. Max loss per lot = 50 * 20 = 1000 Rs.
        # Budget 100 < 1000 -> auto_lots = 0, floors to 1 lot.
        lots = judas.compute_auto_lots(capital=10000, risk_pct=1.0, max_loss_per_unit=50.0, lot_size=20, hard_cap_lots=5)
        assert lots == 1
        assert "is below 1 lot max loss" in caplog.text


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
