#!/bin/bash
# Remove Radio Builder from this computer:  sudo ./uninstall.sh
#
# Stops the web UI if it is running and removes the service, the sudoers rule,
# the `radio` command and /opt/radio-tools. Page backups are kept: the web UI's
# in /var/lib/private/radio-ui/backups, the command line's in ~/radio-backups.
# Companion is not touched: radio groups already on buttons keep working, and
# a Start key on a Companion page simply stops doing anything.
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "error: run it with sudo: sudo $0" >&2; exit 1; }

echo "=== step: stopping radio-ui (if running) ==="
systemctl stop radio-ui.service 2>/dev/null || true

echo "=== step: removing files ==="
rm -f /etc/systemd/system/radio-ui.service /etc/sudoers.d/095-radio-ui
if [ -L /usr/local/bin/radio ] && [ "$(readlink /usr/local/bin/radio)" = /opt/radio-tools/radio.py ]; then
  rm -f /usr/local/bin/radio
fi
rm -rf /opt/radio-tools
systemctl daemon-reload
echo "=== done; backups kept ==="
