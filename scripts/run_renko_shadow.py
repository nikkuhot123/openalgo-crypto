"""Idempotent launcher for the two Renko SHADOW instances.

Runs OUTSIDE the platform scheduler on purpose. These are DRY_RUN instances:
they place no orders and take no locks, so they need none of the platform's
capital or lock coordination -- and launching them this way avoids restarting
the app while POV is live with real money.

The strategy idles outside market hours and resets on each new session, so one
launch persists across days. This script exists for RESILIENCE (reboot, crash),
and is therefore safe to run from cron as often as you like.

Idempotency is by PIDFILE, not by pgrep: `pgrep -af renko_engine_strategy`
reports only the script path, with no UNDERLYING in the command line, so a
pgrep-based check cannot tell the two instances apart and would happily spawn
duplicates of both.
"""
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/opt/openalgo")
from dotenv import load_dotenv

load_dotenv("/opt/openalgo/.env")
from database.auth_db import get_api_key_for_tradingview

RUN_DIR = Path("/opt/openalgo/log/strategies/run")
RUN_DIR.mkdir(parents=True, exist_ok=True)
SCRIPT = "/opt/openalgo/strategies/scripts/renko_engine_strategy.py"


def alive(pid):
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, ValueError):
        return False
    except PermissionError:
        return True
    # confirm it is actually our strategy and not a recycled pid
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="ignore")
    except OSError:
        return False
    return "renko_engine_strategy" in cmd


def main():
    key = get_api_key_for_tradingview("nikhil")
    if not key:
        print("FAIL: no api key in db")
        return 1
    host = (os.getenv("HOST_SERVER") or "").strip().strip("'").strip('"')

    for und in ("SENSEX", "MIDCPNIFTY"):
        pf = RUN_DIR / f"renko_shadow_{und}.pid"
        if pf.exists():
            try:
                pid = int(pf.read_text().strip())
            except ValueError:
                pid = -1
            if alive(pid):
                print(f"{und}: already running (pid {pid})")
                continue
            print(f"{und}: stale pidfile (pid {pid}) -- relaunching")

        env = dict(os.environ)
        env.update({
            "OPENALGO_API_KEY": key,
            "HOST_SERVER": host,
            "UNDERLYING": und,
            "DRY_RUN": "true",
            "STRATEGY_NAME": f"Renko Engine ({und}) SHADOW",
            "MAX_LOTS": "1",
        })
        logf = f"/opt/openalgo/log/strategies/renko_engine_{und.lower()}_shadow.log"
        with open(logf, "a", encoding="utf-8") as fh:
            p = subprocess.Popen(
                ["/opt/openalgo/.venv/bin/python", "-u", SCRIPT],
                env=env, stdout=fh, stderr=subprocess.STDOUT,
                cwd="/opt/openalgo", start_new_session=True)
        pf.write_text(str(p.pid))
        print(f"{und}: started pid {p.pid} -> {logf}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
