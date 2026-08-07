# VPS Log Rotation Incident

A production incident on 2026-08-07 where the `/` partition was found at 86% capacity due to a runaway service log.

---

## 1. The Incident

During routine verification of a code deployment:
- The `/` partition was found with only **6.6 GB free** (86% utilization).
- A single log file `/opt/openalgo/log/openalgo.log` was measured at **16.2 GB** (over 35% of the total disk space).
- If the disk had filled completely, the Gunicorn process would have crashed, potentially freezing active trades or leaving open positions unhedged at the broker.

---

## 2. Root Cause Analysis

1. **Stdout Redirection**: The systemd service file `openalgo.service` redirects both stdout and stderr to the log file via `StandardOutput=append:/opt/openalgo/log/openalgo.log`.
2. **Missing Rotation**: No systemd or `logrotate` configuration existed for this log file. The file grew continuously since the VPS was provisioned.
3. **App Log Config Misconception**: The app's inner `TimedRotatingFileHandler` config was set up to write to daily files (`openalgo_YYYY-MM-DD.log`). However, file logging was disabled in the app's env, forcing all Python loggers to write to stdout/stderr. Gunicorn captured these streams and appended them endlessly to the unrotated systemd log file.
4. **Log Volume**: The logs were dominated by per-poll `Raw Response:` broker dumps (approx. 41,000 lines out of 200,000) and `Found ATM strike:` messages from `option_symbol_service` (approx. 50,000 lines). With nine active strategies running concurrently, the log grew by gigabytes per month.

---

## 3. Resolution

1. **Immediate Space Reclaim**:
   - Archived a 20 MB tail of the log: `tail -c 20M openalgo.log | gzip > openalgo.log.tail.gz`.
   - Truncated the log in-place to 0 bytes: `sudo truncate -s 0 openalgo.log`.
   - *Note: Truncating is safe because systemd opens the file with `O_APPEND`. Writes resume at the new end (offset 0) instead of creating a sparse file. `copytruncate` works on the same principle.*
   - Disk space recovered: **86% -> 51% (22 GB free)**.

2. **Failed Protection Attempt (Inert logrotate)**:
   - A `logrotate` config was installed at `/etc/logrotate.d/openalgo`.
   - Post-install audit revealed **`logrotate` is not installed on this VPS** and there is no systemd logrotate timer. The config was deleted to prevent false security.

3. **Permanent Fix (Hourly systemd cap)**:
   - Installed a lightweight shell script `cap_log.sh` that checks if `openalgo.log` exceeds 200 MB. If it does, it archives a 20 MB tail and truncates the log. It also purges archives older than 7 days.
   - Mounted the script on an hourly systemd timer (`openalgo-logcap.timer`).
   - Verified functionality: simulated a 4 MB file at a 1 MB threshold and confirmed correct truncation.

---

## 4. Current Configuration

- **Timer**: `openalgo-logcap.timer` (Hourly, Persistent=true)
- **Script**: `/opt/openalgo/scripts/cap_log.sh`
- **Max Log Size**: 200 MB (gated on hourly check)
- **Retention**: 7 days of gzip tails
