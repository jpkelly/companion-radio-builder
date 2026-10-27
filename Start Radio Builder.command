#!/bin/bash
# Double-click to start Radio Builder on a Mac. It opens in your browser.
cd "$(dirname "$0")" || exit 1
if ! command -v python3 >/dev/null 2>&1; then
  echo "Radio Builder needs Python 3: https://www.python.org/downloads/"
  read -r -p "Press Return to close."
  exit 1
fi
python3 -I radio-ui.py --bind 127.0.0.1 --open
