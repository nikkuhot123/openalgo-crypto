import os
import sys
import time
import subprocess
from datetime import datetime

# Set environment variables for local execution pointing to the remote VPS API
env = os.environ.copy()
env["OPENALGO_API_KEY"] = "5630fc9f6d72bf997557cd5c89c10cf650ec4c5b13ed78e4ef70f51375fb6b1a"
env["HOST_SERVER"] = "https://openalgo.inikhilesh.com"
env["DRY_RUN"] = "True"  # Local simulation only, NO REAL ORDERS sent to VPS
env["CAPITAL"] = "500000" # 5 Lakhs for Overnight Drift target scaling

def main():
    print("===========================================================================")
    print(" STARTING LOCAL FORWARD TESTING ENGINE (PAPER TRADING)")
    print("===========================================================================")
    print(" Target API       : https://openalgo.inikhilesh.com")
    print(" Simulated Capital: Rs 5,00,000")
    print(" Strategies       : 1. NIFTY Overnight Drift (Futures)")
    print("                    2. VRP Premium Harvester (Options)")
    print("===========================================================================")
    
    # 1. Run Pre-flight Checks
    print("\n[+] Running Pre-flight Check for Overnight Drift...")
    overnight_check = subprocess.run(
        [sys.executable, "strategies/examples/nifty_overnight_drift_strategy.py", "--check"],
        env=env, capture_output=True, text=True
    )
    print(overnight_check.stdout)

    print("\n[+] Running Pre-flight Check for VRP Premium Harvester...")
    vrp_check = subprocess.run(
        [sys.executable, "strategies/examples/vrp_premium_harvester.py", "--check"],
        env=env, capture_output=True, text=True
    )
    print(vrp_check.stdout)

    print("\n[+] Both scripts configured. To run them continuously as independent local")
    print("    processes in dry-run mode, you can spawn them in separate terminals:")
    print("\n    Terminal 1:")
    print("    =========== ")
    print("    set OPENALGO_API_KEY=5630fc9f6d72bf997557cd5c89c10cf650ec4c5b13ed78e4ef70f51375fb6b1a")
    print("    set HOST_SERVER=https://openalgo.inikhilesh.com")
    print("    set DRY_RUN=True")
    print("    set CAPITAL=500000")
    print("    python strategies/examples/nifty_overnight_drift_strategy.py")
    
    print("\n    Terminal 2:")
    print("    =========== ")
    print("    set OPENALGO_API_KEY=5630fc9f6d72bf997557cd5c89c10cf650ec4c5b13ed78e4ef70f51375fb6b1a")
    print("    set HOST_SERVER=https://openalgo.inikhilesh.com")
    print("    set DRY_RUN=True")
    print("    set CAPITAL=500000")
    print("    python strategies/examples/vrp_premium_harvester.py")

if __name__ == "__main__":
    main()
