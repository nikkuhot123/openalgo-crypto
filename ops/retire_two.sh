#!/bin/bash
# Retire two registrations. Both decisions are evidence-backed, not tidying.
#
# red_bar_x_candle_strategy
#   Its own research settled it: PF 1.9 in-sample -> 1.20 full walk-forward ->
#   1.05 on the one window nothing was fitted to, ~Rs 92/trade against a
#   ~Rs 46/lot friction floor. The 2026-08-07 live trade agreed with that
#   verdict rather than changing it: entered 14:17 with 53 minutes of runway,
#   flattened at the 15:10 EOD for -Rs 1,777, the single worst trade of the day.
#   Today's faithful backtest of the fuller PRO Renko engine reached the same
#   place from a different direction -- positive in index points, dead after
#   option friction, and failing cross-symbol and cross-year.
#
# nifty_overnight_drift_strategy
#   It cannot trade and never could. 2026-08-07 15:26:07, verbatim:
#     "computed 0 lots - no entry (capital too small to express target:
#      need ~Rs 5,206,147 for 1 lot at exposure 0.36)"
#   A NIFTY futures lot is ~Rs 18.5 lakh notional and the sizing model wants
#   Rs 52 lakh of capital behind it. It has polled, logged and slept every
#   session since deployment, burning API calls and log volume for nothing.
#
# NOT touched: the .py files stay. Both are committed research and Red Bar's
# gates/shadow mode are referenced by the spec. This drops REGISTRATIONS only,
# exactly as cas_window_logger was handled.
set -e
cd /opt/openalgo

echo "=== pre-flight: nothing must be holding a position ==="
LIVE=$(pgrep -cf 'strategies/scripts/' || echo 0)
echo "strategy processes running: $LIVE"

echo "=== stopping openalgo ==="
sudo systemctl stop openalgo
sleep 3
PIDS=$(pgrep -f 'strategies/scripts/' || true)
[ -n "$PIDS" ] && { kill $PIDS 2>/dev/null || true; sleep 2; }
echo "strategy procs left: $(pgrep -cf 'strategies/scripts/' || echo 0)"

echo "=== dropping the two registrations ==="
.venv/bin/python - <<'PY'
import json, shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

CFG = Path("/opt/openalgo/strategies/strategy_configs.json")
IST = timezone(timedelta(hours=5, minutes=30))
DROP = ["red_bar_x_candle_strategy", "nifty_overnight_drift_strategy"]

cfg = json.loads(CFG.read_text())
bak = CFG.with_suffix(f".json.bak.{datetime.now(IST):%Y%m%d_%H%M%S}")
shutil.copy2(CFG, bak)
print(f"backup -> {bak}")

before = len(cfg)
for k in DROP:
    if k in cfg:
        del cfg[k]
        print(f"  dropped {k}")
    else:
        print(f"  NOT FOUND (already gone): {k}")
CFG.write_text(json.dumps(cfg, indent=2))
print(f"registrations {before} -> {len(cfg)}")
PY

echo "=== restarting openalgo ==="
sudo systemctl start openalgo
sleep 8
systemctl is-active openalgo

echo "=== verify ==="
.venv/bin/python - <<'PY'
import json
from pathlib import Path
cfg = json.loads(Path("/opt/openalgo/strategies/strategy_configs.json").read_text())
print(f"{len(cfg)} registrations remain:")
for k in cfg:
    print("  ", k)
gone = [k for k in ("red_bar_x_candle_strategy", "nifty_overnight_drift_strategy") if k in cfg]
print("STILL PRESENT (should be empty):", gone)
PY
echo "--- http ---"
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:5000/ || true
echo "--- recent errors ---"
tail -200 log/openalgo.log 2>/dev/null | grep -ci error || echo 0
