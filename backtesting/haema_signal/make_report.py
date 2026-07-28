"""
Backtest report for the HA-EMA signal — native openstatz tearsheet -> PDF.

The skill pack has NO native PDF output (the string "pdf" appears in zero files
across both packs); openstatz.reports exposes only basic/full/html/metrics, i.e. a
QuantStats-style HTML tearsheet. So this generates the NATIVE tearsheet HTML and
that file is then printed to PDF with headless Chromium, which keeps openstatz's
own report design instead of a hand-rolled layout.

Data: OpenAlgo (Historify DuckDB via source="db" / API fallback) — the same broker
feed the live strategies trade on.

SCOPE: VectorBT models signals on a price series, so this measures the DIRECTIONAL
EDGE OF THE SIGNAL on the index (a futures-style proxy). It does not model option
strike/premium/theta — see the caveats page in the report.

Usage:
    C:/Users/nikhi/Desktop/openalgo/venv/Scripts/python.exe make_report.py
"""

import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from string import Template
import openstatz as ost
import pandas as pd
import vectorbt as vbt

HERE = Path(__file__).resolve().parent

# reuse the audited backtest module (data loading, HA bias, signal construction)
_spec = importlib.util.spec_from_file_location(
    "haema_bt", HERE / "NIFTY_haema_signal_backtest.py")
bt = importlib.util.module_from_spec(_spec)
sys.modules["haema_bt"] = bt
_spec.loader.exec_module(bt)


def build_portfolio():
    """Rebuild the exact backtest from the audited module's parameters."""
    d5 = bt.fetch(bt.SYMBOL, bt.EXCHANGE, bt.INTERVAL, bt.LOOKBACK_DAYS)
    dd = bt.fetch(bt.SYMBOL, bt.EXCHANGE, "D", bt.LOOKBACK_DAYS)
    close = d5["close"].astype(float)
    high, low = d5["high"].astype(float), d5["low"].astype(float)

    from openalgo import ta
    ema_hi = pd.Series(ta.ema(high, bt.EMA_LEN), index=d5.index)
    ema_lo = pd.Series(ta.ema(low, bt.EMA_LEN), index=d5.index)

    bias = bt.ha_bias_by_day(dd)
    bias_s = pd.Series([bias.get(ts.date()) for ts in d5.index], index=d5.index)

    t = d5.index.time
    in_window = ((t >= pd.Timestamp(bt.ENTRY_START).time())
                 & (t < pd.Timestamp(bt.ENTRY_END).time()))
    is_eod = t >= pd.Timestamp(bt.EOD_EXIT).time()
    if bt.SKIP_EXPIRY_WEEKDAY is not None:
        in_window &= d5.index.weekday != bt.SKIP_EXPIRY_WEEKDAY

    long_raw = (bias_s == "GREEN") & (close > ema_hi) & in_window
    short_raw = (bias_s == "RED") & (close < ema_lo) & in_window

    day = pd.Series(d5.index.date, index=d5.index)

    def first_of_day(s):
        return s & ~s.groupby(day).cumsum().shift(fill_value=0).astype(bool)

    long_e, short_e = first_of_day(long_raw), first_of_day(short_raw)
    clash = long_e & short_e
    long_e, short_e = long_e & ~clash, short_e & ~clash

    pf = vbt.Portfolio.from_signals(
        close=close,
        entries=long_e, exits=pd.Series(is_eod, index=d5.index),
        short_entries=short_e, short_exits=pd.Series(is_eod, index=d5.index),
        sl_stop=bt.SL_PCT, tp_stop=bt.SL_PCT * bt.RR,
        size=bt.LOT_SIZE, init_cash=bt.INIT_CASH,
        fees=bt.FEES, fixed_fees=bt.FIXED_FEES, freq=bt.INTERVAL,
    )
    return pf, close, d5


_COVER = Template("""
<div style="font-family:Arial,Helvetica,sans-serif;color:#111;max-width:950px;margin:24px auto 8px auto;padding:0 14px">
  <h1 style="margin:0 0 2px 0;font-size:22px">HA-EMA 34 Channel Breakout &mdash; Backtest Report</h1>
  <div style="font-size:12.5px;color:#555;margin-bottom:16px">
    NIFTY &middot; 5-minute &middot; data from <b>OpenAlgo</b> (Historify DuckDB &mdash; the same broker feed the live strategies trade on)
  </div>

  <h3 style="margin:16px 0 5px;font-size:15px;border-bottom:1px solid #ccc;padding-bottom:3px">Headline result</h3>
  <table style="font-size:12.5px;border-collapse:collapse;width:100%">
    <tr><td style="padding:3px 6px;width:34%">Period</td><td style="padding:3px 6px"><b>$period</b> &nbsp;($sessions sessions, $bars bars)</td></tr>
    <tr style="background:#f6f6f6"><td style="padding:3px 6px">Trades</td><td style="padding:3px 6px"><b>$trades</b> &nbsp; win rate <b>$win_rate%</b> ($wins W / $losses L)</td></tr>
    <tr><td style="padding:3px 6px">Net P&amp;L</td><td style="padding:3px 6px"><b>Rs $net_pnl</b> &nbsp;=&nbsp; <b>$avg_points index points per trade</b></td></tr>
    <tr style="background:#f6f6f6"><td style="padding:3px 6px">Total return</td><td style="padding:3px 6px"><b>$total_return_pct%</b> &nbsp;vs NIFTY buy &amp; hold <b>$bh_pct%</b></td></tr>
    <tr><td style="padding:3px 6px">Sharpe / Max drawdown</td><td style="padding:3px 6px"><b>$sharpe</b> &nbsp;/&nbsp; <b>$max_dd_pct%</b></td></tr>
  </table>

  <h3 style="margin:18px 0 5px;font-size:15px;border-bottom:1px solid #ccc;padding-bottom:3px">Configuration</h3>
  <table style="font-size:12px;border-collapse:collapse;width:100%">
    <tr><td style="padding:2px 6px;width:34%">Signal</td><td style="padding:2px 6px">previous-day Heikin-Ashi bias gates direction; 34-EMA channel breakout on 5-min highs/lows</td></tr>
    <tr style="background:#f6f6f6"><td style="padding:2px 6px">Risk</td><td style="padding:2px 6px">stop 0.10% of spot (the deployed MIN_SL_PCT floor); target = 2.0 &times; stop</td></tr>
    <tr><td style="padding:2px 6px">Session</td><td style="padding:2px 6px">entries 09:45&ndash;14:30, one per day, square-off 15:15, NIFTY expiry day skipped</td></tr>
    <tr style="background:#f6f6f6"><td style="padding:2px 6px">Costs</td><td style="padding:2px 6px">0.018% + Rs 20/order (F&amp;O futures, ref. Zerodha) &middot; 1 lot = 65 &middot; capital Rs 20,00,000</td></tr>
  </table>

  <h3 style="margin:18px 0 5px;font-size:15px;border-bottom:1px solid #ccc;padding-bottom:3px;color:#8a1f11">Scope limit &mdash; read before using these numbers</h3>
  <div style="font-size:12px;line-height:1.55">
    VectorBT models <b>signals on a price series</b>. The live strategy <b>buys weekly options</b>. This report
    therefore measures the <b>directional edge of the signal on the index</b> (a futures-style proxy) and does
    <b>not</b> model strike selection, premium, theta or gamma. It is still the decisive test: if the signal has
    no directional edge on the index, no option-side tuning can rescue it.
    <br><br>
    Costs applied here are <b>futures-level only</b> &mdash; a fraction of the ~1% option slippage the live system
    actually pays. <b>The signal loses $avg_points index points per trade before option mechanics enter at all.</b>
  </div>

  <h3 style="margin:18px 0 5px;font-size:15px;border-bottom:1px solid #ccc;padding-bottom:3px">Corroborating evidence from other harnesses</h3>
  <table style="font-size:12px;border-collapse:collapse;width:100%">
    <tr><td style="padding:2px 6px;width:34%">Option-level backtest (Volrix)</td><td style="padding:2px 6px">best configuration nets <b>-9.42%</b> after 1% option slippage; every variant sits in an unrecovered drawdown from 2026-04-30 onward</td></tr>
    <tr style="background:#f6f6f6"><td style="padding:2px 6px">Weekday filter (skip Mon+Tue)</td><td style="padding:2px 6px">best in aggregate, then split <b>+7.20%</b> / <b>-10.42%</b> across halves &mdash; regime-dependent, <b>not deployed</b></td></tr>
    <tr><td style="padding:2px 6px">Backtest vs live agreement</td><td style="padding:2px 6px"><b>6 of 12 days</b> on identical logic; live P&amp;L records before 2026-07-28 were themselves faulty (stop-loss exits logging profits)</td></tr>
  </table>

  <div style="font-size:11px;color:#666;margin-top:18px;border-top:1px solid #ddd;padding-top:7px">
    Generated $generated &middot; VectorBT 1.1.0 + openstatz 0.3.0 &middot; data: OpenAlgo Historify DuckDB &middot;
    reproduce with <code>backtesting/haema_signal/make_report.py</code> &middot; full metric tables and charts below.
  </div>
  <hr style="margin:22px 0 4px;border:0;border-top:2px solid #333">
</div>
""")


def inject_cover(html_path: Path, summary: dict):
    """Prepend a cover block to the native openstatz tearsheet.

    The tearsheet carries no scope caveats, and these numbers are actively
    misleading without them (index proxy, not option P&L), so the caveats ship
    inside the report rather than alongside it.
    """
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    block = _COVER.safe_substitute(
        **{k: str(v) for k, v in summary.items()},
        generated=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    marker = "<body>"
    idx = html.lower().find(marker)
    if idx == -1:  # unexpected template: fall back to prepending
        html_path.write_text(block + html, encoding="utf-8")
        return
    cut = idx + len(marker)
    html_path.write_text(html[:cut] + block + html[cut:], encoding="utf-8")


def main():
    pf, close, d5 = build_portfolio()
    tr = pf.trades.records_readable

    # openstatz expects a DAILY returns series; 5-min equity -> daily
    equity = pf.value()
    if getattr(equity.index, "tz", None) is not None:
        equity.index = equity.index.tz_localize(None)
    daily_eq = equity.resample("1D").last().dropna()
    returns = daily_eq.pct_change().dropna()
    returns.name = "HA-EMA signal"

    # buy & hold benchmark on the same index, same daily grid
    bench_px = close.copy()
    if getattr(bench_px.index, "tz", None) is not None:
        bench_px.index = bench_px.index.tz_localize(None)
    bench = bench_px.resample("1D").last().dropna().pct_change().dropna()
    bench.name = "NIFTY buy & hold"

    # ---- summary first: the cover block needs these numbers ----
    n = len(tr)
    pnl = float(tr["PnL"].sum()) if n else 0.0
    wins = int((tr["PnL"] > 0).sum()) if n else 0
    summary = {
        "period": f"{d5.index[0]:%Y-%m-%d} .. {d5.index[-1]:%Y-%m-%d}",
        "bars": len(d5),
        "sessions": len({d.date() for d in d5.index}),
        "trades": n,
        "win_rate": round(100 * wins / n, 1) if n else 0.0,
        "wins": wins,
        "losses": n - wins,
        "net_pnl": round(pnl, 2),
        "avg_per_trade": round(pnl / n, 2) if n else 0.0,
        "avg_points": round(pnl / n / bt.LOT_SIZE, 2) if n else 0.0,
        "total_return_pct": round(float(pf.total_return()) * 100, 2),
        "sharpe": round(float(pf.sharpe_ratio()), 2),
        "max_dd_pct": round(abs(float(pf.max_drawdown())) * 100, 2),
        "bh_pct": round((float(close.iloc[-1]) / float(close.iloc[0]) - 1) * 100, 2),
    }

    html_out = HERE / "NIFTY_haema_signal_tearsheet.html"
    ost.reports.html(
        returns,
        benchmark=bench,
        title="HA-EMA 34 Channel Breakout — NIFTY (OpenAlgo data)",
        output=str(html_out),
        periods_per_year=252,
    )
    inject_cover(html_out, summary)
    print(f"tearsheet HTML -> {html_out}")

    for k, v in summary.items():
        print(f"  {k:18s} {v}")

    (HERE / "report_summary.json").write_text(
        pd.Series(summary).to_json(indent=2), encoding="utf-8")
    if n:
        tr.to_csv(HERE / "NIFTY_haema_signal_trades.csv", index=False)
    print(f"summary JSON   -> {HERE / 'report_summary.json'}")


if __name__ == "__main__":
    main()
