"""Register the two Renko SHADOW instances in the platform so they appear in the
Python Strategies UI.

Must run with the app STOPPED: STRATEGY_CONFIGS is loaded at import and the next
save_configs() writes the in-memory dict back, which would erase a live edit.

Shadow is belt-and-braces here:
  1. env DRY_RUN=true is injected by the launcher, and
  2. the registration NAME contains "SHADOW", which the strategy also honours --
     that is the channel that survives the UI-only path, since the upload form
     never writes the optional env dict.
"""
import json
import shutil
from datetime import datetime
from pathlib import Path

CFG = Path("/opt/openalgo/strategies/strategy_configs.json")
DAYS = ["mon", "tue", "wed", "thu", "fri"]

ENTRIES = {
    "renko_engine_strategy_sensex": {
        "name": "Renko Engine (SENSEX) SHADOW",
        "exchange": "BFO",
        "underlying": "SENSEX",
    },
    "renko_engine_strategy_midcpnifty": {
        "name": "Renko Engine (MIDCPNIFTY) SHADOW",
        "exchange": "NFO",
        "underlying": "MIDCPNIFTY",
    },
}


def main():
    cfg = json.loads(CFG.read_text())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = CFG.with_suffix(f".json.bak.{stamp}")
    shutil.copy2(CFG, bak)
    print(f"backup -> {bak.name}  ({len(cfg)} existing registrations)")

    now = datetime.now().astimezone().isoformat()
    for sid, meta in ENTRIES.items():
        if sid in cfg:
            print(f"  {sid}: already registered, left alone")
            continue
        cfg[sid] = {
            "file_path": "strategies/scripts/renko_engine_strategy.py",
            "file_name": "renko_engine_strategy.py",
            "is_running": False,
            "is_scheduled": True,
            "created_at": now,
            "user_id": "nikhil",
            # 09:16 so the 09:15 X-candle bar exists before the first evaluation
            "schedule_start": "09:16",
            "schedule_stop": "15:20",
            "schedule_days": DAYS,
            "max_lots_nifty": 1,
            "max_lots_sensex": 1,
            "lot_mode": "manual",
            "risk_pct_per_trade": 1.0,
            "env": {"DRY_RUN": "true"},
            "name": meta["name"],
            "exchange": meta["exchange"],
            "underlying": meta["underlying"],
            "pid": None,
            "notes": "Forward test, shadow only. Pass condition: profitable in a "
                     "MAJORITY of forward months (log/strategies/renko_shadow_*.csv). "
                     "Backtest gives 2 of 7 months on real premiums, with March 2026 "
                     "carrying the whole result.",
        }
        print(f"  {sid}: ADDED -> {meta['name']} [{meta['exchange']}]")

    CFG.write_text(json.dumps(cfg, indent=4))
    check = json.loads(CFG.read_text())
    print(f"written: {len(check)} registrations, json valid")
    for k, v in check.items():
        print(f"   {v.get('name'):34s} {v.get('exchange'):4s} "
              f"{v.get('schedule_start')}-{v.get('schedule_stop')} "
              f"sched={v.get('is_scheduled')} manStop={v.get('manually_stopped')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
