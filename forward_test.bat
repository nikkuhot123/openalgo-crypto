@echo off
title OpenAlgo Local Forward Testing
color 0A

:: Set Remote API environment variables
set OPENALGO_API_KEY=5630fc9f6d72bf997557cd5c89c10cf650ec4c5b13ed78e4ef70f51375fb6b1a
set HOST_SERVER=https://openalgo.inikhilesh.com
set DRY_RUN=True
set CAPITAL=500000

echo =========================================================
echo  LOCAL FORWARD TESTING ENGINE (PAPER TRADING)
echo =========================================================
echo  Target API       : %HOST_SERVER%
echo  Simulated Capital: Rs 5,00,000
echo  Dry Run Mode     : %DRY_RUN%
echo =========================================================

echo.
echo Running Pre-flight Checks...
.\venv\Scripts\python.exe strategies\examples\nifty_overnight_drift_strategy.py --check
.\venv\Scripts\python.exe strategies\examples\vrp_premium_harvester.py --check

echo.
echo Launching strategy daemons in separate windows...
echo Close the new windows to stop forward testing.
echo.

:: Launch the Overnight Drift in a new cmd window
start "NIFTY Overnight Drift - Forward Test" cmd /k "set OPENALGO_API_KEY=%OPENALGO_API_KEY%&& set HOST_SERVER=%HOST_SERVER%&& set DRY_RUN=%DRY_RUN%&& set CAPITAL=%CAPITAL%&& .\venv\Scripts\python.exe strategies\examples\nifty_overnight_drift_strategy.py"

:: Launch the VRP Premium Harvester in a new cmd window
start "VRP Premium Harvester - Forward Test" cmd /k "set OPENALGO_API_KEY=%OPENALGO_API_KEY%&& set HOST_SERVER=%HOST_SERVER%&& set DRY_RUN=%DRY_RUN%&& set CAPITAL=%CAPITAL%&& .\venv\Scripts\python.exe strategies\examples\vrp_premium_harvester.py"

echo Successfully launched! You can close this primary window.
pause
