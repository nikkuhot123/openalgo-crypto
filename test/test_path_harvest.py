#!/usr/bin/env python
"""Parser and rule simulator in backtesting/path_harvest.py.

This is the instrument that will decide whether Judas's exit changes, so a
silent defect here (mis-grouped trades, an off-by-one in a rule) would produce
a confident, wrong answer on real money. Ground truth is the 2026-08-07 trade,
whose real premium path is known independently:

    entry 127.50 -> peak 148.50 (+16.5%) -> exit 109.70 (-14.0%)

Break-even must scratch that trade at 0.0 rather than let it run to -14.0.
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backtesting"))

ph = pytest.importorskip("path_harvest")

SYM = "NIFTY11AUG2624600PE"


def _line(ts, prem, entry=127.50):
    pct = (prem - entry) / entry * 100
    return (f"{ts} [INFO] PATH {SYM} prem={prem:.2f} entry={entry:.2f} "
            f"pct={pct:+.1f}% rs={(prem - entry) * 65:+.0f}")


def _write(tmp_path, monkeypatch, lines):
    d = tmp_path / "path_logs"
    d.mkdir()
    (d / "path_lines.txt").write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(ph, "LOGS", d)


def test_parses_judas_format(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        _line("2026-08-07 11:40:31", 127.50),
        _line("2026-08-07 14:15:00", 148.50),
        _line("2026-08-07 15:10:00", 109.70),
    ])
    df = ph.load()
    assert len(df) == 3
    assert df["symbol"].iloc[0] == SYM
    assert df["prem"].tolist() == [127.50, 148.50, 109.70]


def test_parses_pov_format(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        f"2026-08-07 11:48:0{i} [INFO] PATH X ltp={70 + i}.00 entry=70.10 R=3.45 rmult=+0.1"
        for i in range(3)
    ])
    df = ph.load()
    assert len(df) == 3
    assert df["entry"].iloc[0] == 70.10


def test_reproduces_the_known_trade(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        _line("2026-08-07 11:40:31", 127.50),
        _line("2026-08-07 12:30:00", 140.00),
        _line("2026-08-07 14:15:00", 148.50),
        _line("2026-08-07 14:45:00", 124.00),
        _line("2026-08-07 15:10:00", 109.70),
    ])
    trades = ph.to_trades(ph.load())
    assert len(trades) == 1
    t = trades[0]
    entry = float(t["entry"].iloc[0])
    p = t["prem"].values
    assert round((p.max() - entry) / entry * 100, 1) == 16.5
    res = ph.simulate(t, entry)
    assert round(res["current"], 1) == -14.0
    # armed at +5% well before the peak, then premium crossed back under entry
    assert res["be_5pct"] == 0.0
    # the trail gives back 5% from the 148.50 peak rather than all of it
    assert res["trail_5_5"] > 0


def test_break_even_does_not_fire_without_arming(tmp_path, monkeypatch):
    """A trade that never shows +5% must keep its real outcome, not a free 0."""
    _write(tmp_path, monkeypatch, [
        _line("2026-08-07 10:00:00", 127.50),
        _line("2026-08-07 10:30:00", 129.00),   # only +1.2%
        _line("2026-08-07 11:00:00", 110.00),
    ])
    t = ph.to_trades(ph.load())[0]
    res = ph.simulate(t, 127.50)
    assert res["be_5pct"] == res["current"] < 0


def test_new_entry_price_starts_a_new_trade(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        _line("2026-08-07 10:00:00", 100.0, entry=100.0),
        _line("2026-08-07 10:00:30", 101.0, entry=100.0),
        _line("2026-08-07 10:01:00", 102.0, entry=100.0),
        _line("2026-08-07 11:40:00", 127.5, entry=127.5),
        _line("2026-08-07 11:40:30", 130.0, entry=127.5),
        _line("2026-08-07 11:41:00", 132.0, entry=127.5),
    ])
    assert len(ph.to_trades(ph.load())) == 2


def test_a_long_gap_splits_trades(tmp_path, monkeypatch):
    """Same symbol re-entered later the same day is not one long position."""
    _write(tmp_path, monkeypatch, [
        _line("2026-08-07 10:00:00", 127.5),
        _line("2026-08-07 10:00:30", 128.0),
        _line("2026-08-07 10:01:00", 129.0),
        _line("2026-08-07 13:00:00", 127.5),
        _line("2026-08-07 13:00:30", 126.0),
        _line("2026-08-07 13:01:00", 125.0),
    ])
    assert len(ph.to_trades(ph.load())) == 2


def test_noise_lines_are_ignored(tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, [
        "2026-08-07 11:40:31 [INFO] Monitoring Trade: X | Spot: 24578.60",
        "not a log line at all",
        _line("2026-08-07 11:40:31", 127.50),
    ])
    assert len(ph.load()) == 1


def test_gate_threshold_is_the_agreed_one():
    assert ph.MIN_TRADES == 15


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
