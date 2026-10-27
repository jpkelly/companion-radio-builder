#!/bin/bash
# Install Radio Builder on the computer that runs Companion (Linux with systemd).
#
#   sudo ./install.sh [--port 8088] [--companion 127.0.0.1:8000] [--protect-pages 99] [--companion-user companion]
#
#   --port            port for the web UI (default 8088)
#   --companion       Companion's web address as host:port (default 127.0.0.1:8000)
#   --protect-pages   pages the web UI may show but never change, e.g. 99 or 98,99; "" for none (default 99)
#   --companion-user  user that runs Companion; it gets permission to start/stop the web UI (default companion)
#
# Copies the tool to /opt/radio-tools, adds the `radio` command, installs an
# on-demand systemd service (radio-ui.service, never started at boot) and, if
# the Companion user exists, ONE sudoers rule so Companion's shell-command keys
# may start/stop exactly that service. Nothing is started and Companion is not
# touched. Running it again updates an existing install.
set -euo pipefail

PORT=8088
COMPANION=127.0.0.1:8000
PROTECT=99
COMPANION_USER=companion
HERE="$(cd "$(dirname "$0")" && pwd)"

usage() { sed -n '3,9p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:?--port needs a number}"; shift 2 ;;
    --companion) COMPANION="${2:?--companion needs host:port}"; shift 2 ;;
    --protect-pages) PROTECT="${2?--protect-pages needs a list such as 99, or \"\" for none}"; shift 2 ;;
    --companion-user) COMPANION_USER="${2:?--companion-user needs a user name}"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "unknown option: $1" >&2; usage 2 ;;
  esac
done

fail() { echo "error: $*" >&2; exit 1; }
[[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || fail "--port must be a number from 1 to 65535"
[[ "$COMPANION" =~ ^[A-Za-z0-9.-]+:[0-9]+$ ]] || fail "--companion must look like host:port, e.g. 127.0.0.1:8000"
[[ "$PROTECT" =~ ^[0-9,]*$ ]] || fail "--protect-pages must be page numbers separated by commas"
[[ "$COMPANION_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || fail "--companion-user is not a valid user name"

echo "=== step: checking this computer ==="
[ "$(id -u)" -eq 0 ] || fail "run it with sudo: sudo $0"
SYSTEMCTL="$(command -v systemctl)" || fail "systemd (systemctl) is required"
[ -x /usr/bin/python3 ] || fail "/usr/bin/python3 is required"
/usr/bin/python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || fail "Python 3.8 or newer is required"
for f in radio-patch.py radio.py radio-ui.py; do
  [ -f "$HERE/$f" ] || fail "$f is missing next to install.sh"
done

echo "=== step: tool files -> /opt/radio-tools ==="
install -d -m 755 -o root -g root /opt/radio-tools
install -m 644 -o root -g root "$HERE/radio-patch.py" "$HERE/radio-ui.py" /opt/radio-tools/
install -m 755 -o root -g root "$HERE/radio.py" /opt/radio-tools/
/usr/bin/python3 -I -m py_compile /opt/radio-tools/radio-patch.py /opt/radio-tools/radio.py /opt/radio-tools/radio-ui.py 2>/dev/null || true
ln -sf /opt/radio-tools/radio.py /usr/local/bin/radio

echo "=== step: systemd unit (no [Install] section: never starts at boot) ==="
cat > /etc/systemd/system/radio-ui.service <<UNIT
[Unit]
Description=Radio Builder (Companion radio-group web UI, started on demand)
After=network.target

[Service]
Type=simple
DynamicUser=yes
StateDirectory=radio-ui
Environment=RADIO_BACKUP_DIR=/var/lib/radio-ui/backups
Environment=RADIO_BLOCKED_PAGES=${PROTECT}
ExecStart=/usr/bin/python3 -I /opt/radio-tools/radio-ui.py --port ${PORT} --companion ${COMPANION} --idle-minutes 30
Restart=no
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
MemoryMax=200M
UNIT
"$SYSTEMCTL" daemon-reload
systemd-analyze verify /etc/systemd/system/radio-ui.service 2>&1 | head -5 || true

echo "=== step: sudoers rule for the '${COMPANION_USER}' user ==="
if id "$COMPANION_USER" >/dev/null 2>&1; then
  RULE="$(mktemp)"
  printf '%s\n' "${COMPANION_USER} ALL=NOPASSWD: ${SYSTEMCTL} start radio-ui.service, ${SYSTEMCTL} stop radio-ui.service" > "$RULE"
  visudo -cf "$RULE" >/dev/null
  install -m 440 -o root -g root "$RULE" /etc/sudoers.d/095-radio-ui
  rm -f "$RULE"
  # check only our own file: a global `visudo -c` also trips over other packages' files
  visudo -cf /etc/sudoers.d/095-radio-ui >/dev/null
  echo "added /etc/sudoers.d/095-radio-ui"
else
  echo "no user '${COMPANION_USER}' here, so no Start key permission was added (see --companion-user)"
fi

if "$SYSTEMCTL" is-active --quiet radio-ui.service; then
  echo "note: radio-ui is running the previous version until it stops (sudo systemctl stop radio-ui)"
fi

cat <<DONE
=== done ===
Start the web UI:  sudo systemctl start radio-ui
Then open:         http://$(hostname).local:${PORT}   (or this computer's IP address)
It stops by itself after 30 idle minutes. Command line: radio --help
Optional Start key in Companion: action "System: Run shell command (local)" with
  sudo ${SYSTEMCTL} start radio-ui.service
(needs Companion's shell commands switched on; see README).
DONE
