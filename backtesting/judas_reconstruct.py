"""
Can a missing premium path be reconstructed? Validate before believing it.
==========================================================================
The exit question for Judas needs premium paths, but the broker drops expired
contracts: 4 of 27 round trips survive, and those 4 are simply the most RECENT
four -- three of which are losers. Fitting an exit rule to that is fitting to a
bad fortnight.

The inputs to rebuild the missing ones are all still available:
  - index 1-minute path      (indices never expire; fetch one day at a time,
                              the range-request path is corrupt -- 83a7c162d)
  - strike / expiry / right  (parsed from the traded symbol)
  - implied vol              (India VIX IS a NIFTY IV index, and these are ATM)

So price Black-Scholes along the real spot path and you get a modelled premium
path including theta, which is the whole quantity of interest.

This file does NOT run the exit study. It answers one prior question: does the
reconstruction track reality? Ground truth is the 4 real premium paths plus
every entry fill recoverable from the logs. If it does not track, the honest
answer is to wait for the PATH logs shipped on 2026-08-07 and this file gets
deleted.

Usage:
    ./venv/Scripts/python.exe backtesting/judas_reconstruct.py
"""
import math
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
ROOT = HERE.parent

MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
YEAR = 365.0 * 24 * 60  # minutes, for T
IDX = {"NFO": ("NIFTY", "NSE_INDEX"), "BFO": ("SENSEX", "BSE_INDEX")}


def client():
    from openalgo import api

    env = (ROOT / ".env").read_text()
    return api(
        api_key=env.split("OPENALGO_API_KEY=")[1].split()[0],
        host=env.split("OPENALGO_HOST=")[1].split()[0],
    )


def parse_symbol(sym):
    """NIFTY11AUG2624600PE -> (underlying, expiry_date, strike, right)."""
    m = re.match(r"^([A-Z]+)(\d{2})([A-Z]{3})(\d{2})(\d+)(CE|PE)$", sym)
    if not m:
        return None
    u, dd, mon, yy, strike, right = m.groups()
    return u, datetime(2000 + int(yy), MONTHS[mon], int(dd), 15, 30), float(strike), right


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(spot, strike, t_years, vol, right):
    if t_years <= 0 or vol <= 0:
        intrinsic = (spot - strike) if right == "CE" else (strike - spot)
        return max(intrinsic, 0.0)
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * t_years) / (vol * math.sqrt(t_years))
    d2 = d1 - vol * math.sqrt(t_years)
    if right == "CE":
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_vol(price, spot, strike, t_years, right, lo=0.01, hi=3.0):
    """Bisection. Returns None if the price is outside the model's range."""
    for _ in range(80):
        mid = (lo + hi) / 2
        if bs_price(spot, strike, t_years, mid, right) < price:
            lo = mid
        else:
            hi = mid
    v = (lo + hi) / 2
    return v if 0.02 < v < 2.9 else None


def day_series(c, symbol, exchange, day, interval="1m"):
    d = c.history(symbol=symbol, exchange=exchange, interval=interval,
                  start_date=day, end_date=day)
    if not isinstance(d, pd.DataFrame) or d.empty:
        return None
    d = d.copy()
    d.index = pd.to_datetime(d.index).tz_localize(None)
    return d


def reconstruct(c, sym, exchange, entry_ts, exit_ts, anchor_price=None, vix_cache={}):
    """Modelled premium path. anchor_price re-solves IV so the model starts on
    the real entry fill; without it, India VIX is used directly."""
    p = parse_symbol(sym)
    if not p:
        return None
    _u, expiry, strike, right = p
    und, und_exch = IDX.get(exchange, ("NIFTY", "NSE_INDEX"))
    day = pd.Timestamp(entry_ts).strftime("%Y-%m-%d")

    spot = day_series(c, und, und_exch, day)
    if spot is None:
        return None
    seg = spot[(spot.index >= pd.Timestamp(entry_ts).floor("min"))
               & (spot.index <= pd.Timestamp(exit_ts).ceil("min"))]
    if len(seg) < 3:
        return None

    if day not in vix_cache:
        v = day_series(c, "INDIAVIX", "NSE_INDEX", day)
        vix_cache[day] = (float(v["close"].mean()) / 100.0) if v is not None else None
    vol = vix_cache[day]
    if vol is None:
        return None

    t0 = (expiry - pd.Timestamp(entry_ts).to_pydatetime()).total_seconds() / 60.0 / YEAR
    if anchor_price:
        solved = implied_vol(anchor_price, float(seg["close"].iloc[0]), strike, t0, right)
        if solved:
            vol = solved

    rows = []
    for ts, row in seg.iterrows():
        t = max((expiry - ts.to_pydatetime()).total_seconds() / 60.0 / YEAR, 0.0)
        rows.append(
            {
                "ts": ts,
                "high": bs_price(float(row["high"] if right == "CE" else row["low"]), strike, t, vol, right),
                "low": bs_price(float(row["low"] if right == "CE" else row["high"]), strike, t, vol, right),
                "close": bs_price(float(row["close"]), strike, t, vol, right),
            }
        )
    out = pd.DataFrame(rows).set_index("ts")
    out.attrs["vol"] = vol
    return out


def main():
    orders = pd.read_csv(HERE / "judas_orders.csv", parse_dates=["ts"])
    trades, open_pos = [], {}
    for _, r in orders.sort_values("ts").iterrows():
        if r["action"] == "BUY":
            open_pos.setdefault(r["symbol"], []).append(r)
        elif open_pos.get(r["symbol"]):
            b = open_pos[r["symbol"]].pop(0)
            trades.append({"symbol": r["symbol"], "exchange": r["exchange"],
                           "entry_ts": b["ts"], "exit_ts": r["ts"], "qty": b["qty"]})
    trades = pd.DataFrame(trades)

    real = pd.read_csv(HERE / "judas_premium_paths.csv", parse_dates=["entry_ts"])
    print(f"{len(trades)} round trips | {len(real)} with a real premium path (ground truth)\n")

    c = client()
    print("VALIDATION -- modelled vs real, on the paths that still exist")
    print(f"{'symbol':22s} {'real MFE':>9s} {'model MFE':>10s} {'real end':>9s} "
          f"{'model end':>10s} {'corr':>6s} {'MAE%':>6s}")
    checks = []
    for _, t in real.iterrows():
        row = trades[(trades["symbol"] == t["symbol"])
                     & (trades["entry_ts"] == t["entry_ts"])]
        if row.empty:
            continue
        r0 = row.iloc[0]
        model = reconstruct(c, t["symbol"], r0["exchange"], r0["entry_ts"], r0["exit_ts"],
                            anchor_price=float(t["entry"]))
        if model is None:
            print(f"{t['symbol']:22s}  reconstruction failed")
            continue
        e = float(model["close"].iloc[0])
        m_mfe = (model["high"].max() - e) / e * 100
        m_end = (model["close"].iloc[-1] - e) / e * 100
        # real path, re-fetched for a like-for-like series comparison
        rp = day_series(c, t["symbol"], r0["exchange"],
                        pd.Timestamp(r0["entry_ts"]).strftime("%Y-%m-%d"))
        corr = mae = float("nan")
        if rp is not None:
            rp = rp[(rp.index >= pd.Timestamp(r0["entry_ts"]).floor("min"))
                    & (rp.index <= pd.Timestamp(r0["exit_ts"]).ceil("min"))]
            j = model.join(rp[["close"]], rsuffix="_real", how="inner").dropna()
            if len(j) > 3:
                corr = float(np.corrcoef(j["close"], j["close_real"])[0, 1])
                mae = float((j["close"] - j["close_real"]).abs().mean() / e * 100)
        checks.append({"mfe_err": m_mfe - t["mfe_pct"], "end_err": m_end - t["current"],
                       "corr": corr, "mae": mae})
        print(f"{t['symbol']:22s} {t['mfe_pct']:+8.1f}% {m_mfe:+9.1f}% "
              f"{t['current']:+8.1f}% {m_end:+9.1f}% {corr:6.2f} {mae:5.1f}%")

    if not checks:
        print("\nno overlap to validate against -- cannot trust reconstruction")
        return 1
    cd = pd.DataFrame(checks)
    print(
        f"\nMFE error  mean {cd['mfe_err'].mean():+.1f}pp  abs-mean {cd['mfe_err'].abs().mean():.1f}pp"
        f"\nend error  mean {cd['end_err'].mean():+.1f}pp  abs-mean {cd['end_err'].abs().mean():.1f}pp"
        f"\ncorrelation mean {cd['corr'].mean():.2f} | MAE mean {cd['mae'].mean():.1f}% of entry"
    )
    ok = cd["corr"].mean() > 0.90 and cd["mfe_err"].abs().mean() < 4.0
    print(
        "\nVERDICT: reconstruction tracks reality -- safe to expand the sample."
        if ok else
        "\nVERDICT: reconstruction does NOT track reality closely enough.\n"
        "Do not expand the sample with it; wait for the live PATH logs."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
