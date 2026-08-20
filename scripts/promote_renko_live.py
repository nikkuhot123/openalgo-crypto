"""Promote both Renko registrations from SHADOW to LIVE.

Must run with the app STOPPED.

Changes:
  1. Rename "Renko Engine (SENSEX) SHADOW" -> "Renko Engine (SENSEX)"
  2. Rename "Renko Engine (MIDCPNIFTY) SHADOW" -> "Renko Engine (MIDCPNIFTY)"
  3. env: {"DRY_RUN": "false"} on both
  4. Clear manually_stopped (if set) so they auto-start on schedule

The strategy honours BOTH: dropping SHADOW from the name clears the in-name
gate, and DRY_RUN=false clears the env gate. Both must agree for live trading.
"""
import json
import shutil
from datetime import datetime
from pathlib import Path

CFG = Path("/opt/openalgo/strategies/strategy_configs.json")


def main():
    cfg = json.loads(CFG.read_text())
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = CFG.with_suffix(f".json.bak.{stamp}")
    shutil.copy2(CFG, bak)
    print(f"backup -> {bak.name}")

    targets = {
        "renko_engine_strategy_sensex": "Renko Engine (SENSEX)",
        "renko_engine_strategy_midcpnifty": "Renko Engine (MIDCPNIFTY)",
    }

    for sid, new_name in targets.items():
        if sid not in cfg:
            print(f"  ERROR: {sid} not in configs")
            return 1
        entry = cfg[sid]
        old_name = entry.get("name")
        entry["name"] = new_name
        entry["env"] = {"DRY_RUN": "false"}
        entry.pop("manually_stopped", None)
        entry["is_scheduled"] = True
        # Notes reflect the real-money state
        entry["notes"] = (
            f"LIVE, 1 lot, 15m. Swept exits: stop prev candle, T1 2.5R (50%), "
            f"T2 3.0R, max 2/day. Promoted from shadow {datetime.now().date()}."
        )
        print(f"  {sid}: {old_name!r} -> {new_name!r} | DRY_RUN=false")

    CFG.write_text(json.dumps(cfg, indent=4))
    check = json.loads(CFG.read_text())
    print(f"\nwritten: {len(check)} registrations, json valid")
    for sid in targets:
        v = check[sid]
        print(f"   {v.get('name'):32s} {v.get('exchange'):4s} "
              f"env={v.get('env')} sched={v.get('is_scheduled')} "
              f"manStop={v.get('manually_stopped')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
