#!/bin/bash
# Cap the service log without adding a package.
#
# The /etc/logrotate.d/openalgo written earlier is INERT: logrotate is not
# installed on this box and there is no logrotate timer, so that file protected
# nothing. Removing it rather than leaving a false sense of safety.
#
# This is a size-gated truncate on an hourly systemd timer. truncate -s 0 is
# correct here because the unit opens the file with O_APPEND
# (StandardOutput=append:...), so writes resume at the new end rather than
# re-creating a sparse file at the old offset.
set -e

echo "=== removing the inert logrotate config ==="
sudo rm -f /etc/logrotate.d/openalgo && echo "removed"

echo "=== installing the cap script ==="
sudo mkdir -p /opt/openalgo/scripts
sudo tee /opt/openalgo/scripts/cap_log.sh >/dev/null <<'SCRIPT'
#!/bin/bash
# Keep /opt/openalgo/log/openalgo.log under MAX. Archive a tail, then truncate.
set -e
F=/opt/openalgo/log/openalgo.log
MAX=$((200 * 1024 * 1024))
KEEP=7
[ -f "$F" ] || exit 0
SZ=$(stat -c%s "$F")
[ "$SZ" -le "$MAX" ] && exit 0
tail -c 20M "$F" | gzip > "${F}.$(date +%Y%m%d_%H%M%S).gz"
truncate -s 0 "$F"
ls -t ${F}.*.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
logger -t openalgo-logcap "capped openalgo.log at ${SZ} bytes"
SCRIPT
sudo chmod +x /opt/openalgo/scripts/cap_log.sh

echo "=== installing the timer ==="
sudo tee /etc/systemd/system/openalgo-logcap.service >/dev/null <<'UNIT'
[Unit]
Description=Cap the openalgo service log

[Service]
Type=oneshot
ExecStart=/opt/openalgo/scripts/cap_log.sh
UNIT

sudo tee /etc/systemd/system/openalgo-logcap.timer >/dev/null <<'UNIT'
[Unit]
Description=Hourly cap of the openalgo service log

[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now openalgo-logcap.timer

echo "=== verify ==="
sudo /opt/openalgo/scripts/cap_log.sh && echo "cap script runs clean (no-op while under 200M)"
systemctl list-timers openalgo-logcap.timer --no-pager | head -3
df -h / | tail -1
echo "service: $(systemctl is-active openalgo)"
