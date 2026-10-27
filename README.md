# Companion Radio Builder

Make Bitfocus Companion keys work like radio buttons: press one key in a group
and it lights up, and the others go back to normal.

## Start

You need Python 3. Download the ZIP (**Code** → **Download ZIP**), unzip it,
and in that folder run:

```sh
python3 radio-ui.py --companion <companion-ip>:8000
```

On Windows, use `py` instead of `python3`. Then open <http://localhost:8088>.

## Use

1. Load a page.
2. Click the keys for a group, type a name, and click **Put selected keys in this group**.
3. Pick the colours, click **Review changes**, then apply.

**Undo last change** puts the page back. Applying replaces the page in
Companion, so don't do it during a show.

## Install on a Companion Pi (optional)

From the downloaded folder:

```sh
bash install-remote.sh pi@<pi-name>.local
```

When it finishes, it shows how to start the tool.
