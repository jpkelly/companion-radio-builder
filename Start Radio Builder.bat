@echo off
rem Double-click to start Radio Builder on Windows. It opens in your browser.
cd /d "%~dp0"
where py >/dev/null 2>nul
if %errorlevel%==0 (
  py -3 -I radio-ui.py --bind 127.0.0.1 --open
) else (
  python -I radio-ui.py --bind 127.0.0.1 --open
)
if errorlevel 1 (
  echo.
  echo Radio Builder stopped with an error. If Python is missing, install it from https://www.python.org/downloads/
  pause
)
