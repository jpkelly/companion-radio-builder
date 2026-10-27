# Companion Radio Builder

Make Bitfocus Companion keys work like radio buttons: press one key in a group
and it lights up, and the others go back to normal.

## Quick start

1. Install [Python 3](https://www.python.org/downloads/).
2. Download this repository (**Code** → **Download ZIP**) and unzip it.
3. In that folder, run `python3 radio-ui.py --companion <companion-ip>:8000`.
4. Open <http://localhost:8088>.

If Companion runs on the same computer, leave out `--companion ...`. Press
Ctrl+C to stop.

## macOS

1. Install Python 3 from [python.org](https://www.python.org/downloads/).
2. Download the ZIP and double-click it to unzip.
3. Open Terminal and run:
   ```sh
   cd ~/Downloads/companion-radio-builder-main
   python3 radio-ui.py --companion <companion-ip>:8000
   ```
4. Open <http://localhost:8088>.

## Windows

1. Install Python 3 from [python.org](https://www.python.org/downloads/).
2. Download the ZIP, right-click it and choose **Extract All**.
3. Open the extracted folder that contains `radio-ui.py`, type `cmd` in the
   address bar and press Enter.
4. Run:
   ```bat
   py radio-ui.py --companion <companion-ip>:8000
   ```
5. Open <http://localhost:8088>.

## Linux

```sh
cd ~/Downloads
unzip companion-radio-builder-main.zip
cd companion-radio-builder-main
python3 radio-ui.py --companion <companion-ip>:8000
```

Open <http://localhost:8088>.

## Companion Pi

This installs the tool on the Pi, ready to start at any time. Log in to the Pi
(ssh) and run:

```sh
curl -L https://github.com/jpkelly/companion-radio-builder/archive/refs/heads/main.tar.gz | tar xz
cd companion-radio-builder-main
sudo bash install.sh
sudo systemctl start radio-ui
```

Open `http://<pi-name>.local:8088`. The tool stops after 30 idle minutes. To
start it again, run `sudo systemctl start radio-ui`.

## Use

1. Load a page.
2. Click the keys for a group, type a name, and click **Put selected keys in this group**.
3. Pick the colours, click **Review changes**, then apply.

**Undo last change** puts the page back. Applying replaces the page in
Companion, so don't do it during a show.
