#!/usr/bin/python3 -I
"""radio - add or remove radio-group behaviour on a Companion page, in one command.

Run it on the Pi that runs Companion (needs only Python 3, no extra packages).

  radio list  PAGE
  radio apply PAGE -g A=0:1,0:2,0:3 [-g B=1:1,1:2] [--on-bg C] [--on-text C|auto] [--on-border C] [--off-bg C] [--off-text C|auto] [--yes] [--dry-run]
  radio strip PAGE [--keys 0:1,0:2] [--yes] [--dry-run]
  radio load  FILE PAGE [--yes]            put a saved page export on PAGE (also: undo)

Keys are "row:column", counted from 0. `apply` and `strip` do everything in one
go: read the live page, save a backup, patch it, ask for confirmation, re-check
the page has not changed, import it, read it back and verify. Backups go to
~/radio-backups (override with RADIO_BACKUP_DIR). If anything looks wrong the
backup can be put back with `radio load <backup file> PAGE`.

Importing replaces the whole page, so anything using that page's buttons can be
briefly disturbed. Set RADIO_ALLOWED_PAGES=19,20 to restrict which pages this
tool may replace. Companion is found at 127.0.0.1:8000 (override: --host or
RADIO_HOST).
"""
import argparse
import base64
import gzip
import hashlib
import importlib.util
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("radio_patch", HERE / "radio-patch.py")
rp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rp)

CHUNK = 256 * 1024


class RadioError(Exception):
    pass


# ---------------------------------------------------------------- minimal tRPC-over-WebSocket client

class Trpc:
    """Just enough of RFC 6455 + tRPC's JSON protocol to call mutations."""

    def __init__(self, hostport, timeout=20):
        host, _, port = hostport.partition(":")
        self.sock = socket.create_connection((host, int(port or 80)), timeout=timeout)
        self.buf = b""
        self.next_id = 1
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            (
                f"GET /trpc HTTP/1.1\r\nHost: {hostport}\r\nUpgrade: websocket\r\n"
                f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        head = self._read_until(b"\r\n\r\n")
        status = head.split(b"\r\n")[0].decode(errors="replace")
        if " 101 " not in status:
            raise RadioError(f"websocket upgrade refused by Companion: {status}")

    def _read_until(self, marker):
        while marker not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RadioError("connection closed during handshake")
            self.buf += chunk
        head, _, self.buf = self.buf.partition(marker)
        return head

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise RadioError("connection closed by Companion")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _send_frame(self, opcode, payload):
        n = len(payload)
        hdr = bytearray([0x80 | opcode])
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126)
            hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127)
            hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _recv_message(self):
        parts = []
        while True:
            b1, b2 = self._recv_exact(2)
            fin, op, n = b1 & 0x80, b1 & 0x0F, b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            mask = self._recv_exact(4) if b2 & 0x80 else None
            payload = self._recv_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if op == 9:  # ping
                self._send_frame(0xA, payload)
                continue
            if op == 10:  # pong
                continue
            if op == 8:
                raise RadioError("Companion closed the connection")
            parts.append(payload)
            if fin:
                return b"".join(parts)

    def call(self, path, input_=None):
        mid = self.next_id
        self.next_id += 1
        params = {"path": path}
        if input_ is not None:
            params["input"] = input_
        self._send_frame(1, json.dumps({"id": mid, "method": "mutation", "params": params}).encode())
        while True:
            text = self._recv_message().decode()
            if text == "PING":  # tRPC keep-alive
                self._send_frame(1, b"PONG")
                continue
            m = json.loads(text)
            if m.get("id") != mid:
                continue
            if "error" in m:
                raise RadioError(f"{path}: {m['error'].get('message', m['error'])}")
            result = m.get("result", {})
            if result.get("type") == "data":
                return result.get("data")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- Companion access

TESTED_VERSION = (5, 2)  # the Companion release this tool is built and tested against


def companion_version(export):
    """(major, minor, patch) of the Companion that made `export`, or None if it does not say."""
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(export.get("companionBuild") or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def check_version(export):
    """Refuse a Companion older than 5 (its pages use another format).

    Returns a warning for any version other than the tested one, else None.
    """
    v = companion_version(export)
    if v is None:
        return "Could not tell which Companion version this is. Radio Builder needs Companion 5 and is tested with 5.2."
    shown = ".".join(map(str, v))
    if v[0] < 5:
        raise RadioError(f"this is Companion {shown}; Radio Builder needs Companion 5 or newer")
    if v[:2] != TESTED_VERSION:
        return f"This is Companion {shown}; Radio Builder is tested with 5.2. Check the keys after your first change."
    return None


def fetch_page(hostport, page):
    try:
        with urllib.request.urlopen(f"http://{hostport}/int/export/page/{page}", timeout=20) as r:
            raw = r.read()
    except OSError as e:
        raise RadioError(f"cannot read page {page} from {hostport}: {e}")
    try:
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        d = json.loads(raw)
    except (OSError, ValueError):
        raise RadioError(f"{hostport} did not answer like Companion; check the address and port")
    check_version(d)
    return d


def import_page(hostport, page_export, target, remap):
    """Replace page `target` with `page_export` (a single-page export dict)."""
    raw = gzip.compress(json.dumps(page_export).encode())
    t = Trpc(hostport)
    try:
        sid = t.call("importExport.prepareImport.start", {"name": "radio.companionconfig", "size": len(raw)})
        for off in range(0, len(raw), CHUNK):
            t.call(
                "importExport.prepareImport.uploadChunk",
                {"sessionId": sid, "offset": off, "data": base64.b64encode(raw[off : off + CHUNK]).decode()},
            )
        err, summary = t.call(
            "importExport.prepareImport.complete",
            {"sessionId": sid, "expectedChecksum": hashlib.sha1(raw).hexdigest(), "userData": None},
        )
        if err:
            raise RadioError(f"Companion could not parse the page: {err}")
        if summary.get("type") != "page":
            raise RadioError(f"Companion parsed a {summary.get('type')!r} export, expected a single page")
        t.call(
            "importExport.importSinglePage",
            {"targetPage": target, "sourcePage": summary.get("oldPageNumber") or 1, "connectionIdRemapping": remap},
        )
    finally:
        try:
            t.call("importExport.abort")
        except (RadioError, OSError):
            pass
        t.close()


# ---------------------------------------------------------------- workflow

def count_controls(d):
    return sum(len(row) for row in d["page"]["controls"].values())


def check_allowed(page):
    allowed = os.environ.get("RADIO_ALLOWED_PAGES")
    if allowed and page not in [int(x) for x in allowed.split(",") if x.strip()]:
        raise RadioError(f"page {page} is not in RADIO_ALLOWED_PAGES ({allowed})")


def backup_dir():
    configured = os.environ.get("RADIO_BACKUP_DIR")
    # only look up the home directory when needed: a service user may not have one
    return Path(configured) if configured else Path.home() / "radio-backups"


def save_backup(before, page, kind="before"):
    d = backup_dir()
    d.mkdir(parents=True, exist_ok=True)
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
    path = d / f"{stamp}-page{page}-{kind}.json"
    path.write_text(json.dumps(before, indent=1))
    return path


BACKUP_NAME = re.compile(r"^\d{8}-\d{6}-\d{3}-page(\d+)-before\.json$")


def list_backups(page):
    """Backups taken before a change to `page`, newest first (undo backups excluded)."""
    d = backup_dir()
    if not d.is_dir():
        return []
    names = [p.name for p in d.iterdir() if (m := BACKUP_NAME.match(p.name)) and int(m.group(1)) == page]
    return sorted(names, reverse=True)


def blocked_pages():
    """Pages the UI must never replace (default: 99, the utility page)."""
    return [int(x) for x in os.environ.get("RADIO_BLOCKED_PAGES", "99").split(",") if x.strip()]


def check_blocked(page):
    if page in blocked_pages():
        raise RadioError(f"page {page} is protected and cannot be changed by this tool")


def fetch_connection_ids(host):
    """Ids of every connection that exists on this Companion (reads the full export once)."""
    try:
        with urllib.request.urlopen(f"http://{host}/int/export/full", timeout=60) as r:
            raw = r.read()
    except OSError as e:
        raise RadioError(f"cannot read the connection list from {host}: {e}")
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return set(json.loads(raw).get("instances", {}))


def check_connections(host, live, new):
    """Connection ids used by `new` must exist; returns the identity remapping for the import."""
    ids = list(new.get("instances", {}))
    missing = [i for i in ids if i not in live.get("instances", {})]
    if missing:  # the target page may simply not use them yet: ask for all live connections
        known = fetch_connection_ids(host)
        missing = [i for i in missing if i not in known]
    if missing:
        raise RadioError(f"connections {missing} do not exist on this Companion; refusing to import")
    return {i: i for i in ids}


def _swap_page(host, page, before, new):
    """Re-check the live page is unchanged, import `new`, return the page read back."""
    live = fetch_page(host, page)
    if live["page"] != before["page"]:
        raise RadioError(f"page {page} was edited while we were working; nothing imported. Try again.")
    import_page(host, new, page, check_connections(host, live, new))
    return fetch_page(host, page)


def sync_live(host, page, desired, style=None, dry_run=False):
    """Make page `page` carry exactly the radio assignments in `desired`.

    desired: {(row, col): label} or {(row, col): (label, style_or_None)}; style is the
    default for new buttons (None = the library default). Raises RadioError/PatchError on refusal.
    """
    check_blocked(page)
    check_allowed(page)
    style = style or rp.DEFAULT_STYLE
    before = fetch_page(host, page)
    new, summary = rp.sync_radio(before, desired, style)
    changes = summary["added"] + summary["changed"] + summary["removed"]
    result = {"ok": True, "page": page, "name": before["page"]["name"], "summary": summary, "dry_run": dry_run}
    if changes == 0:
        result["message"] = "Nothing to change."
        return result
    if dry_run:
        result["message"] = f"{changes} button(s) would change."
        return result
    backup = save_backup(before, page)
    after = _swap_page(host, page, before, new)
    _, left = rp.sync_radio(after, desired, style)  # a correct read-back needs no further changes
    settled = left["added"] + left["changed"] + left["removed"] == 0
    untouched = rp.same_ignoring_ids(rp.strip_radio(after)[0], rp.strip_radio(before)[0])
    result["backup"] = backup.name
    result["ok"] = settled and untouched
    result["message"] = (
        f"Done: {changes} button(s) changed and verified."
        if result["ok"]
        else "The page was replaced but the read-back does not match what was intended. Use Undo to restore the backup."
    )
    return result


def restore_live(host, page, name=None):
    """Put back a backup (newest if `name` is None) on `page`; the current page is backed up first."""
    check_blocked(page)
    check_allowed(page)
    names = list_backups(page)
    if name is None:
        if not names:
            raise RadioError(f"no backups for page {page}")
        name = names[0]
    m = BACKUP_NAME.match(name)
    if not m or int(m.group(1)) != page or name not in names:
        raise RadioError(f"{name!r} is not a backup of page {page}")
    new = rp.load_page(backup_dir() / name)
    before = fetch_page(host, page)
    save_backup(before, page, "before-undo")
    after = _swap_page(host, page, before, new)
    ok = rp.same_ignoring_ids(after, new)
    return {
        "ok": ok,
        "page": page,
        "restored": name,
        "message": f"Restored {name}." if ok else "Restored, but the read-back does not match the backup.",
    }


def confirm(question, assume_yes):
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        raise RadioError("not a terminal: pass --yes to proceed without asking")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def replace_page(host, page, before, new, summary, args):
    """Back up, confirm, re-check, import. Returns (backup path, live page after) or None if declined."""
    check_allowed(page)
    backup = save_backup(before, page)
    print(f"backup saved: {backup}")
    print(f"page {page} ({before['page']['name']!r}) will be REPLACED: {count_controls(before)} controls -> {count_controls(new)}")
    print(summary)
    if getattr(args, "dry_run", False):
        out = backup.with_name(backup.name.replace("-before", "-proposed"))
        out.write_text(json.dumps(new, indent=1))
        print(f"dry run: proposed page written to {out}; nothing imported")
        return None
    if not confirm("Replace the page now?", args.yes):
        print("cancelled; nothing changed")
        return None
    live = fetch_page(host, page)
    if live["page"] != before["page"]:
        raise RadioError(f"page {page} was edited while we were working; nothing imported. Run the command again.")
    import_page(host, new, page, check_connections(host, live, new))
    return backup, fetch_page(host, page)


def report(ok, backup, page, details):
    if ok:
        print(f"VERIFIED: {details}")
        return 0
    print(f"MISMATCH: {details}")
    print(f"restore the previous page with:  radio load {backup} {page}")
    return 1


def note_version(d):
    warning = check_version(d)
    if warning:
        print(f"warning: {warning}", file=sys.stderr)


def cmd_list(a):
    d = fetch_page(a.host, a.page)
    note_version(d)
    groups = rp.list_radio(d)
    print(f"page {a.page} {d['page']['name']!r}: radio groups -> {rp._fmt(groups)}")
    return 0


def cmd_apply(a):
    before = fetch_page(a.host, a.page)
    note_version(before)
    groups = dict(rp.parse_group_spec(s) for s in a.group)
    new = rp.apply_radio(before, groups, rp.style_from_args(a))
    done = replace_page(a.host, a.page, before, new, f"adding radio groups: {rp._fmt(groups)}", a)
    if done is None:
        return 0
    backup, after = done
    wanted = {g: sorted(k) for g, k in groups.items()}
    got = {g: sorted(k) for g, k in rp.list_radio(after).items()}
    untouched = rp.same_ignoring_ids(rp.strip_radio(after)[0], before)
    return report(got == wanted and untouched, backup, a.page,
                  f"groups {rp._fmt(rp.list_radio(after))}; everything else unchanged: {untouched}")


def cmd_strip(a):
    before = fetch_page(a.host, a.page)
    note_version(before)
    new, n = rp.strip_radio(before, rp.parse_keys(a.keys) if a.keys else None)
    if n == 0:
        print("no radio parts found on that page; nothing to do")
        return 0
    done = replace_page(a.host, a.page, before, new, f"removing radio parts from {n} buttons", a)
    if done is None:
        return 0
    backup, after = done
    same = rp.same_ignoring_ids(after, new)
    return report(same, backup, a.page, f"remaining groups {rp._fmt(rp.list_radio(after))}")


def cmd_load(a):
    new = rp.load_page(a.file)
    before = fetch_page(a.host, a.page)
    note_version(before)
    done = replace_page(a.host, a.page, before, new, f"loading {a.file}", a)
    if done is None:
        return 0
    backup, after = done
    return report(rp.same_ignoring_ids(after, new), backup, a.page, f"page now has {count_controls(after)} controls")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=os.environ.get("RADIO_HOST", "127.0.0.1:8000"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("list"); p.add_argument("page", type=int)
    p = sub.add_parser("apply"); p.add_argument("page", type=int)
    p.add_argument("-g", "--group", action="append", required=True, metavar="LABEL=row:col,...")
    for flag, text in (("--on-bg", "background when selected, #rrggbb"),
                       ("--on-text", "text colour when selected, #rrggbb or auto"),
                       ("--on-border", "border colour when selected, #rrggbb"),
                       ("--off-bg", "background when not selected, #rrggbb"),
                       ("--off-text", "text colour when not selected, #rrggbb or auto")):
        p.add_argument(flag, help=text)
    p = sub.add_parser("strip"); p.add_argument("page", type=int); p.add_argument("--keys")
    p = sub.add_parser("load"); p.add_argument("file"); p.add_argument("page", type=int)
    for name in ("apply", "strip", "load"):
        sp = sub.choices[name]
        sp.add_argument("--yes", action="store_true", help="do not ask for confirmation")
        if name != "load":
            sp.add_argument("--dry-run", action="store_true", help="show the plan and write the proposed page; import nothing")
    a = ap.parse_args(argv)
    try:
        return {"list": cmd_list, "apply": cmd_apply, "strip": cmd_strip, "load": cmd_load}[a.cmd](a)
    except (RadioError, rp.PatchError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
