#!/bin/bash
# Reclaim the runaway service log and make it impossible to recur.
#
# Found 2026-08-07 while verifying the strategy retirement: / was 86% full with
# 6.6G free and /opt/openalgo/log/openalgo.log alone was 16G -- a third of the
# disk. If it fills, gunicorn dies mid-session, potentially holding a position.
#
# Cause: the unit sends BOTH streams to one file with no rotation --
#   StandardOutput=append:/opt/openalgo/log/openalgo.log
#   StandardError=append:/opt/openalgo/log/openalgo.log
# and no /etc/logrotate.d entry exists. The app's own TimedRotatingFileHandler
# is not the culprit; it writes dated openalgo_YYYY-MM-DD.log files, and none
# exist here, so file logging is off and everything funnels to stdout.
#
# Volume is dominated by per-poll INFO chatter -- of the last 200k lines,
# ~41k were broker "Raw Response:" dumps and ~50k were option_symbol_service
# ATM-strike lookups. Nine strategies polling every 5-15s all session.
#
# truncate -s 0 is safe here precisely BECAUSE systemd opened the file with
# O_APPEND: writes continue at the new end instead of re-creating a sparse
# file at the old offset. logrotate uses copytruncate for the same reason --
# systemd's fd survives a rename and would keep writing to the rotated inode.
set -e

echo "=== before ==="
df -h / | tail -1
du -h /opt/openalgo/log/openalgo.log | cut -f1

echo "=== archiving a tail for reference, then truncating ==="
sudo tail -c 20M /opt/openalgo/log/openalgo.log \
  | gzip > /opt/openalgo/log/openalgo.log.tail-20260807.gz
sudo truncate -s 0 /opt/openalgo/log/openalgo.log
echo "archived: $(du -h /opt/openalgo/log/openalgo.log.tail-20260807.gz | cut -f1)"

echo "=== installing logrotate ==="
sudo tee /etc/logrotate.d/openalgo >/dev/null <<'CONF'
/opt/openalgo/log/openalgo.log {
    daily
    size 200M
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
}
/opt/openalgo/log/strategies/*.log {
    weekly
    rotate 4
    compress
    missingok
    notifempty
    copytruncate
}
CONF
sudo logrotate -d /etc/logrotate.d/openalgo 2>&1 | grep -E 'considering|log needs|rotating' | head -6

echo "=== after ==="
df -h / | tail -1
echo "service: $(systemctl is-active openalgo)"
curl -s -o /dev/null -w "http %{http_code}\n" http://127.0.0.1:5000/ || true
