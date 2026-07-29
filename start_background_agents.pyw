import subprocess
import sys
import os

os.makedirs("log", exist_ok=True)
env = os.environ.copy()
env["OPENALGO_API_KEY"] = "5630fc9f6d72bf997557cd5c89c10cf650ec4c5b13ed78e4ef70f51375fb6b1a"
env["HOST_SERVER"] = "https://openalgo.inikhilesh.com"
env["DRY_RUN"] = "True"
env["CAPITAL"] = "500000"

def launch_daemon(script_path, log_file_name):
    log_file = open(f"log/{log_file_name}", "a")
    python_path = sys.executable.replace("python.exe", "pythonw.exe")
    
    process = subprocess.Popen(
        [python_path, script_path],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        creationflags=0x08000000,
        env=env
    )
    return process

def main():
    print("Launching Overnight Drift Strategy in background...")
    p1 = launch_daemon("strategies/examples/nifty_overnight_drift_strategy.py", "fw_test_nifty_overnight.log")
    
    print("Launching VRP Premium Harvester in background...")
    p2 = launch_daemon("strategies/examples/vrp_premium_harvester.py", "fw_test_vrp_harvester.log")
    
    print("\n[SUCCESS] Both background agents are now running continuously!")
    print(" - Overnight Log: C:/Users/nikhi/Desktop/openalgo/log/fw_test_nifty_overnight.log")
    print(" - VRP Log      : C:/Users/nikhi/Desktop/openalgo/log/fw_test_vrp_harvester.log")
    print(" - Ledger       : C:/Users/nikhi/Desktop/openalgo/log/strategies/state/vrp_paper_ledger.csv")

if __name__ == "__main__":
    main()
