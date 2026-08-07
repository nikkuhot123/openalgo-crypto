"""
Collect the PATH lines the live strategies now emit, and report progress to n>=15.
==================================================================================
The Judas exit decision was deferred on 2026-08-07 for lack of sample: only 4
of 27 round trips still had a premium path, and reconstructing the rest from
Black-Scholes failed validation (see judas_reconstruct.py). Judas and POV now
log the premium every cycle while holding, so the sample builds forward.

Run this any day to see whether the data is actually arriving. Do NOT wait two
weeks and then discover the collector was silent -- that is precisely how
prior_levels_ema burned a week entering nothing.

Once n >= MIN_TRADES, this replays the same exit rules as judas_premium_exit.py
on the collected paths, which unlike the 4 survivors are a forward, unbiased
sample.

Usage:
    ./venv/Scripts/python.exe backtesting/path_harvest.py            # local logs
    ./venv/Scripts/python.exe backtesting/path_harvest.py --pull     # fetch from VPS first
"""
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).parent
LOGS = HERE / "path_logs"
MIN_TRADES = 15
VPS = "ubuntu@80.225.250.15"
KEY = "~/.ssh/vps_deploy_key"

# Judas: PATH SYM prem=148.50 entry=127.50 pct=+16.5% rs=+1365
# POV:   PATH SYM ltp=74.60 entry=70.10 R=3.45 rmult=+1.30
RE_JUDAS = re.compile(r"PATH (\S+) prem=([\d.]+) entry=([\d.]+)")
RE_POV = re.compile(r"PATH (\S+) ltp=([\d.]+) entry=([\d.]+)")
RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def pull():
    LOGS.mkdir(exist_ok=True)
    cmd = (
        f'ssh -i {KEY} -o StrictHostKeyChecking=no -o BatchMode=yes {VPS} '
        f'"cd /opt/openalgo/log/strategies && grep -h PATH '
        f'judas_swing_strategy*.log pov_wall_squeeze_strategy*.log 2>/dev/null"'
    )
    out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=180)
    # grep exits 1 when nothing matches, which is the normal state until the
    # first trade is held. Only a real SSH/transport error is a failure, and it
    # MUST be loud: a silently-empty pull looks identical to "no trades yet"
    # and would hide a broken collector for the whole two-week window.
    if out.returncode not in (0, 1) or out.stderr.strip():
        print(f"PULL FAILED (collector status unknown): rc={out.returncode} "
              f"{out.stderr.strip()[:200]}")
        return None
    if not out.stdout.strip():
        print("connected OK; no PATH lines on the VPS yet (no trade held since deploy)")
    dest = LOGS / "path_lines.txt"
    dest.write_text(out.stdout, encoding="utf-8")
    print(f"pulled {len(out.stdout.splitlines())} PATH lines -> {dest}")
    return dest


def load():
    f = LOGS / "path_lines.txt"
    if not f.exists():
        return pd.DataFrame()
    rows = []
    for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
        ts = RE_TS.match(line)
        m = RE_JUDAS.search(line) or RE_POV.search(line)
        if not (ts and m):
            continue
        rows.append(
            {
                "ts": pd.Timestamp(ts.group(1)),
                "symbol": m.group(1),
                "prem": float(m.group(2)),
                "entry": float(m.group(3)),
            }
        )
    return pd.DataFrame(rows)


def to_trades(df):
    """Group PATH samples into trades. A new (symbol, entry) pair, or a gap of
    more than 30 minutes, starts a new trade."""
    trades, cur, key = [], [], None
    for _, r in df.sort_values("ts").iterrows():
        k = (r["symbol"], round(r["entry"], 2))
        gap = cur and (r["ts"] - cur[-1]["ts"]).total_seconds() > 1800
        if k != key or gap:
            if len(cur) >= 3:
                trades.append(pd.DataFrame(cur))
            cur, key = [], k
        cur.append(r)
    if len(cur) >= 3:
        trades.append(pd.DataFrame(cur))
    return trades


def simulate(path, entry):
    """Same rule set as judas_premium_exit.py, on sampled (not OHLC) premiums."""
    p = path["prem"].values
    out = {"current": (p[-1] - entry) / entry * 100.0}
    for a in (5.0, 8.0, 12.0):
        armed, res = False, None
        for x in p:
            if not armed and (x - entry) / entry * 100.0 >= a:
                armed = True
            elif armed and x <= entry:
                res = 0.0
                break
        out[f"be_{a:g}pct"] = res if res is not None else out["current"]
    for a, g in ((5.0, 5.0), (8.0, 5.0), (8.0, 8.0)):
        peak, armed, res = entry, False, None
        for x in p:
            peak = max(peak, x)
            if not armed and (peak - entry) / entry * 100.0 >= a:
                armed = True
            if armed and x <= peak * (1 - g / 100.0):
                res = (peak * (1 - g / 100.0) - entry) / entry * 100.0
                break
        out[f"trail_{a:g}_{g:g}"] = res if res is not None else out["current"]
    return out


def main():
    if "--pull" in sys.argv:
        pull()
    df = load()
    if df.empty:
        print("no PATH lines yet. Run with --pull, and check a trade has been held\n"
              "since the 2026-08-07 deploy (logging starts on the first held cycle).")
        return 0

    trades = to_trades(df)
    print(f"{len(df)} PATH samples -> {len(trades)} trades "
          f"({df['ts'].min():%Y-%m-%d} .. {df['ts'].max():%Y-%m-%d})")

    rows = []
    for t in trades:
        entry = float(t["entry"].iloc[0])
        p = t["prem"].values
        rec = {
            "symbol": t["symbol"].iloc[0],
            "start": t["ts"].iloc[0],
            "samples": len(t),
            "mins": round((t["ts"].iloc[-1] - t["ts"].iloc[0]).total_seconds() / 60),
            "entry": round(entry, 2),
            "mfe_pct": round((p.max() - entry) / entry * 100, 1),
            "mae_pct": round((p.min() - entry) / entry * 100, 1),
        }
        rec.update({k: round(v, 2) for k, v in simulate(t, entry).items()})
        rows.append(rec)
    d = pd.DataFrame(rows)
    print(d[["symbol", "start", "samples", "mins", "entry", "mfe_pct", "mae_pct", "current"]]
          .to_string(index=False))

    n = len(d)
    print(f"\nprogress: {n}/{MIN_TRADES} trades toward the pre-registered gate")
    if n < MIN_TRADES:
        print(f"  need {MIN_TRADES - n} more. Do not act on these yet -- the whole\n"
              f"  point of the gate is that small samples argued for whatever the\n"
              f"  last bad week looked like.")
        return 0

    cols = [c for c in d.columns if c.startswith(("be_", "trail_"))] + ["current"]
    tab = pd.DataFrame({
        "mean_pct": d[cols].mean().round(2),
        "median_pct": d[cols].median().round(2),
        "win_rate": (d[cols] > 0).mean().round(2),
        "worst_pct": d[cols].min().round(2),
    }).sort_values("mean_pct", ascending=False)
    print("\nexit rules on the forward sample:")
    print(tab.to_string())

    best = tab.index[0]
    if best != "current":
        diff = (d[best] - d["current"]).values
        rng = np.random.default_rng(0)
        boot = [rng.choice(diff, len(diff), replace=True).mean() for _ in range(10000)]
        lo, hi = np.percentile(boot, [2.5, 97.5])
        print(f"\npaired: {best} minus current (n={len(diff)})")
        print(f"  mean {diff.mean():+.2f}pp  95% CI [{lo:+.2f}, {hi:+.2f}]  "
              f"better {int((diff > 0).sum())} / worse {int((diff < 0).sum())}")
        print("\nGATE: ship only if the CI excludes zero AND the winner is a"
              "\nbreak-even rather than a trail -- a BE at entry cannot cap a"
              "\nrunner, which is why BE beat trailing on the 25-trade spot study.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
