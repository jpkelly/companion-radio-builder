#!/bin/bash
# Install Radio Builder on another computer over ssh, from a copy of this repo.
#
#   ./install-remote.sh [user@]host [install.sh options...]
#   e.g. ./install-remote.sh pi@companion.local --port 8088
#
# Copies the three tool files and install.sh to a temporary folder on the host,
# runs `sudo ./install.sh` there with the given options, then removes the
# folder. The ssh user needs sudo rights; you may be asked for its password.
set -euo pipefail

TARGET="${1:?usage: install-remote.sh [user@]host [install.sh options...]}"
shift
HERE="$(cd "$(dirname "$0")" && pwd)"
ARGS=""
[ $# -gt 0 ] && ARGS="$(printf '%q ' "$@")"

echo "=== step: copying files to ${TARGET} ==="
REMOTE_DIR="$(ssh -o ConnectTimeout=8 "$TARGET" 'mktemp -d /tmp/radio-install.XXXXXX')"
scp -q -o ConnectTimeout=8 "$HERE/radio-patch.py" "$HERE/radio.py" "$HERE/radio-ui.py" "$HERE/install.sh" "${TARGET}:${REMOTE_DIR}/"

echo "=== step: running install.sh on ${TARGET} ==="
ssh -t -o ConnectTimeout=8 "$TARGET" "cd ${REMOTE_DIR} && sudo bash ./install.sh ${ARGS}; rc=\$?; rm -rf ${REMOTE_DIR}; exit \$rc"
