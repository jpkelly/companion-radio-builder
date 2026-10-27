#!/usr/bin/python3 -I
"""Radio Builder - a small web UI for putting buttons into radio groups.

Open it from a browser (default http://<this pi>:8088). Pick a page, click the
keys that belong together, give them a group label, review the changes and
apply. Every change backs the page up first and can be undone.

Run by hand:   python3 -I radio-ui.py [--port 8088] [--idle-minutes 30]
It stops itself after --idle-minutes without a request. Pages listed in
RADIO_BLOCKED_PAGES (default: 99) can be viewed but never changed.
"""
import argparse
import importlib.util
import json
import re
import sys
import threading
import time
import traceback
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("radio", HERE / "radio.py")
radio = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(radio)
rp = radio.rp

KEY_RE = re.compile(r"^(\d+):(\d+)$")
MAX_BODY = 1 << 20


def label_of(button):
    for layer in button["style"]["layers"]:
        if layer.get("type") == "text":
            text = str(layer.get("text", {}).get("value", ""))
            return " ".join(text.replace("\\n", " ").split())[:40]
    return ""


def fetch_text(companion, page, row, col):
    """The text Companion actually renders on a key (variables resolved), or None if unavailable."""
    url = f"http://{companion}/api/variable/internal/b_text_{page}_{row}_{col}/value"
    try:
        with urllib.request.urlopen(url, timeout=3) as r:
            return r.read().decode("utf-8", "replace")
    except (OSError, ValueError):
        return None


def page_view(companion, page):
    d = radio.fetch_page(companion, page)
    p = d["page"]
    grid = p["gridSize"]
    groups = {key: label for label, keys in rp.list_radio(d).items() for key in keys}
    cells = []
    for r, row in p["controls"].items():
        for c, ctl in row.items():
            cell = {"row": r, "col": c, "kind": "button" if ctl["type"] == "button-layered" else "other"}
            if cell["kind"] == "button":
                cell["text"] = label_of(ctl)
                cell["group"] = groups.get((r, c))
                st = rp.style_of(ctl) if cell["group"] else None
                cell["style"] = rp.style_to_json(st) if st is not None else None
            else:
                cell["text"] = ctl["type"]
            cells.append(cell)
    # show what each key really displays: ask Companion for the rendered text, keep the template as a tooltip
    buttons = [c for c in cells if c["kind"] == "button"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        shown = list(pool.map(lambda c: fetch_text(companion, page, c["row"], c["col"]), buttons))
    for cell, text in zip(buttons, shown):
        cell["template"] = cell["text"]
        if text is not None:
            cell["text"] = " ".join(text.replace("\\n", " ").split())[:40]  # the literal two-character \n marker too
    return {
        "ok": True,
        "page": page,
        "name": p["name"],
        "minRow": grid["minRow"],
        "maxRow": grid["maxRow"],
        "minColumn": grid["minColumn"],
        "maxColumn": grid["maxColumn"],
        "cells": cells,
        "backups": len(radio.list_backups(page)),
        "blocked": page in radio.blocked_pages(),
    }


def parse_assignments(raw):
    """{"r:c": "A"} or {"r:c": {"group": "A", "style": {"on": {...}, "off": {...}} | null}}  (see rp.parse_style)."""
    if not isinstance(raw, dict):
        raise radio.RadioError("assignments must be an object")
    out = {}
    for key, value in raw.items():
        m = KEY_RE.match(key)
        if not m:
            raise radio.RadioError(f"bad assignment {key!r}")
        if isinstance(value, str):
            entry = value
        elif isinstance(value, dict) and isinstance(value.get("group"), str):
            st = value.get("style")
            if st is None:
                style = None
            elif isinstance(st, dict):
                style = rp.parse_style(st)
            else:
                raise radio.RadioError(f"bad style for {key!r}")
            entry = (value["group"], style)
        else:
            raise radio.RadioError(f"bad assignment {key!r}")
        out[(m.group(1), m.group(2))] = entry
    return out


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, companion, idle_seconds):
        super().__init__(addr, Handler)
        self.companion = companion
        self.idle_seconds = idle_seconds
        self.last_activity = time.time()
        self.op_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "RadioBuilder"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype != "application/json" else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _fail(self, code, message):
        self._send(code, {"ok": False, "error": message})

    def do_GET(self):
        self.server.last_activity = time.time()
        path = urlparse(self.path).path
        if path == "/":
            return self._send(200, PAGE_HTML.encode(), "text/html")
        if path == "/api/status":
            return self._send(200, {"ok": True, "blocked": radio.blocked_pages(), "idle_seconds": self.server.idle_seconds})
        m = re.match(r"^/api/page/(\d+)$", path)
        if m:
            try:
                return self._send(200, page_view(self.server.companion, int(m.group(1))))
            except radio.RadioError as e:
                return self._fail(400, str(e))
            except Exception:
                traceback.print_exc()
                return self._fail(500, "unexpected error; see the service log")
        return self._fail(404, "not found")

    def do_POST(self):
        self.server.last_activity = time.time()
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host", ""):
            return self._fail(403, "cross-site request refused")
        if self.headers.get("X-Radio-UI") != "1" or "application/json" not in self.headers.get("Content-Type", ""):
            return self._fail(403, "missing headers")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            if n > MAX_BODY:
                return self._fail(413, "request too large")
            body = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, OSError):
            return self._fail(400, "bad JSON")
        path = urlparse(self.path).path
        if path == "/api/stop":
            self._send(200, {"ok": True, "message": "Stopping."})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if path not in ("/api/sync", "/api/restore"):
            return self._fail(404, "not found")
        if not self.server.op_lock.acquire(blocking=False):
            return self._fail(409, "another change is in progress; try again in a moment")
        try:
            page = body.get("page")
            if not isinstance(page, int) or page < 1:
                raise radio.RadioError("bad page number")
            if path == "/api/sync":
                result = radio.sync_live(
                    self.server.companion, page, parse_assignments(body.get("assignments")), dry_run=bool(body.get("dry_run"))
                )
            else:
                result = radio.restore_live(self.server.companion, page, body.get("name"))
            self._send(200, result)
        except (radio.RadioError, rp.PatchError) as e:
            self._fail(400, str(e))
        except Exception:  # keep the service alive, log the detail, tell the user briefly
            traceback.print_exc()
            self._fail(500, "unexpected error; see the service log")
        finally:
            self.server.op_lock.release()


def watch_idle(srv, check_every=5):
    while True:
        time.sleep(check_every)
        if time.time() - srv.last_activity > srv.idle_seconds and not srv.op_lock.locked():
            print("idle timeout reached; stopping", file=sys.stderr, flush=True)
            srv.shutdown()
            return


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--companion", default="127.0.0.1:8000", help="Companion host:port")
    ap.add_argument("--idle-minutes", type=float, default=30)
    a = ap.parse_args(argv)
    srv = Server((a.bind, a.port), a.companion, a.idle_minutes * 60)
    threading.Thread(target=watch_idle, args=(srv,), daemon=True).start()
    print(f"Radio Builder on port {srv.server_address[1]}, Companion at {a.companion}, "
          f"stops after {a.idle_minutes:g} idle minutes, protected pages: {radio.blocked_pages()}", file=sys.stderr, flush=True)
    try:
        srv.serve_forever()
    finally:
        srv.server_close()
    return 0


PAGE_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Radio Builder</title>
<style>
 body{font:15px system-ui,sans-serif;background:#14171c;color:#e6e8eb;margin:0;padding:18px;max-width:900px}
 h1{margin:0 0 4px;font-size:22px} .sub{color:#9aa3ad;margin:0 0 16px}
 .bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0}
 input{background:#1e232b;color:inherit;border:1px solid #3a424d;border-radius:6px;padding:7px 9px;font:inherit}
 #page{width:70px} #label{width:120px}
 button{background:#2a313b;color:inherit;border:1px solid #46505d;border-radius:6px;padding:8px 12px;font:inherit;cursor:pointer}
 button:hover{background:#343c48} button.primary{background:#1f6f43;border-color:#2a9a5d}
 button:disabled{opacity:.4;cursor:not-allowed}
 #grid{display:grid;gap:6px;margin:12px 0}
 .cell{height:86px;border:2px solid #2c333d;border-radius:8px;padding:5px;position:relative;overflow:hidden;box-sizing:border-box}
 .cell.empty{border-style:dashed;opacity:.35} .cell.other{display:flex;align-items:center;justify-content:center;color:#7d8793;font-size:12px}
 .cell.btn{background:#1e232b;cursor:pointer} .cell.sel{outline:3px solid #f0c040}
 .cell .t{font-size:12px;line-height:1.2;max-height:44px;overflow:hidden;word-break:break-word}
 .cell .loc{position:absolute;left:6px;bottom:4px;font-size:11px;color:#7d8793}
 .cell .badge{position:absolute;right:5px;bottom:4px;border-radius:5px;padding:1px 7px;font-weight:600;font-size:13px}
 .cell .swatch{position:absolute;right:6px;width:14px;height:14px;border-radius:3px;border:2px solid #fff}
 .cell .swatch.on{top:5px} .cell .swatch.off{top:25px;border-color:#8a929c}
 fieldset#look{border:1px solid #3a424d;border-radius:8px;margin:10px 0;padding:8px 12px}
 fieldset#look legend{color:#9aa3ad;font-size:13px} .lrow{margin:6px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
 .lrow strong{width:105px} select{background:#1e232b;color:inherit;border:1px solid #3a424d;border-radius:6px;padding:6px 8px;font:inherit}
 input[type=color]{padding:0;width:38px;height:30px}
 .cell.dirty .loc::after{content:" *";color:#f0c040}
 #legend span{display:inline-block;margin:0 8px 6px 0;padding:2px 9px;border-radius:5px;font-size:13px}
 #plan{background:#1e232b;border:1px solid #3a424d;border-radius:8px;padding:12px;margin:12px 0}
 .warn{background:#4a3410;border:1px solid #b9872a;border-radius:8px;padding:10px;margin:10px 0}
 .good{color:#6fd49a} .bad{color:#ff8a80} #msg{min-height:22px;margin-top:8px}
</style></head><body>
<h1>Radio Builder</h1>
<p class="sub">Keys in the same group behave like radio buttons: pressing one highlights it and clears the others in its group.</p>
<div class="bar">Page <input id="page" type="number" min="1" max="99" value="1"> <button id="load">Load</button> <strong id="pname"></strong></div>
<div id="warn" class="warn" hidden></div>
<div id="grid"></div>
<div id="legend"></div>
<div class="bar">1. Click keys to select them &nbsp; 2. Group label <input id="label" maxlength="20" placeholder="A">
 <button id="assign">Put selected keys in this group</button> <button id="clear">Take selected keys out of their group</button> <button id="none">Deselect all</button></div>
<fieldset id="look"><legend>Look given to the keys you put in the group (leave a field on “Keep” to use the button’s own)</legend>
 <div class="lrow"><strong>Selected</strong>
  Background <select id="on_bg_m"><option value="keep">Keep</option><option value="color">Colour…</option></select><input id="on_bg_c" type="color" value="#1f8f4a">
  Text <select id="on_text_m"><option value="keep">Keep</option><option value="auto">Auto (readable)</option><option value="color">Colour…</option></select><input id="on_text_c" type="color" value="#ffffff">
  Border <select id="on_border_m"><option value="none">None</option><option value="color" selected>Colour…</option></select><input id="on_border_c" type="color" value="#ffcc00"></div>
 <div class="lrow"><strong>Not selected</strong>
  Background <select id="off_bg_m"><option value="keep">Keep</option><option value="color">Colour…</option></select><input id="off_bg_c" type="color" value="#202020">
  Text <select id="off_text_m"><option value="keep">Keep</option><option value="auto">Auto (readable)</option><option value="color">Colour…</option></select><input id="off_text_c" type="color" value="#ffffff"></div>
</fieldset>
<div class="bar">3. <button id="review" class="primary">Review changes</button> <button id="undo">Undo last change</button> <button id="stop">Stop this tool</button></div>
<div id="plan" hidden></div>
<div id="msg"></div>
<script>
"use strict";
const $ = id => document.getElementById(id);
let view = null, cur = {}, sel = new Set();
const hue = s => { let h = 0; for (const ch of s) h = (h * 31 + ch.charCodeAt(0)) % 360; return h; };

async function api(path, body) {
  const opt = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json", "X-Radio-UI": "1"}, body: JSON.stringify(body)};
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({ok: false, error: "unreadable response"}));
  if (!r.ok || j.ok === false) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}
function say(text, bad) { const m = $("msg"); m.textContent = text; m.className = bad ? "bad" : "good"; }
function orig(k) { const c = view.cells.find(c => c.row + ":" + c.col === k); return c && c.group ? {g: c.group, s: c.style} : null; }
const FIELDS = ["on_bg", "on_text", "on_border", "off_bg", "off_text"];
function fieldValue(f) { const m = $(f + "_m").value; return m === "color" ? $(f + "_c").value : (m === "auto" ? "auto" : null); }
function readStyle() {
  return {on: {bg: fieldValue("on_bg"), text: fieldValue("on_text"), border: fieldValue("on_border")},
          off: {bg: fieldValue("off_bg"), text: fieldValue("off_text")}};
}
function showColours() { for (const f of FIELDS) $(f + "_c").hidden = $(f + "_m").value !== "color"; }
for (const f of FIELDS) $(f + "_m").onchange = showColours;
const norm = s => s ? JSON.stringify([s.on.bg, s.on.text, s.on.border, s.off.bg, s.off.text].map(v => v && v.toLowerCase())) : "";
function same(a, b) {
  if (!a || !b) return !a && !b;
  return a.g === b.g && norm(a.s) === norm(b.s);
}
function describe(s) {
  const part = (name, v) => v ? name + " " + v : null;
  const on = [part("bg", s.on.bg), part("text", s.on.text), part("border", s.on.border)].filter(Boolean).join(", ");
  const off = [part("bg", s.off.bg), part("text", s.off.text)].filter(Boolean).join(", ");
  return "selected: " + (on || "own look") + "; not selected: " + (off || "own look");
}
function payload() {
  const out = {};
  for (const [k, v] of Object.entries(cur)) out[k] = {group: v.g, style: v.s};
  return out;
}

function render() {
  const g = $("grid"); g.replaceChildren();
  const cols = view.maxColumn - view.minColumn + 1;
  g.style.gridTemplateColumns = "repeat(" + cols + ", minmax(0, 96px))";
  const cells = new Map(view.cells.map(c => [c.row + ":" + c.col, c]));
  for (let r = view.minRow; r <= view.maxRow; r++) for (let c = view.minColumn; c <= view.maxColumn; c++) {
    const k = r + ":" + c, cell = cells.get(k), d = document.createElement("div");
    d.className = "cell";
    if (!cell) d.classList.add("empty");
    else if (cell.kind !== "button") { d.classList.add("other"); d.textContent = cell.text; }
    else {
      d.classList.add("btn");
      const t = document.createElement("div"); t.className = "t"; t.textContent = cell.text || "(no text)";
      if (cell.template && cell.template !== cell.text) d.title = "Label template: " + cell.template;
      const loc = document.createElement("div"); loc.className = "loc"; loc.textContent = k;
      d.append(t, loc);
      if (cur[k]) {
        const v = cur[k];
        const b = document.createElement("div"); b.className = "badge"; b.textContent = v.g;
        b.style.background = "hsl(" + hue(v.g) + " 55% 38%)"; d.append(b);
        d.style.borderColor = "hsl(" + hue(v.g) + " 55% 45%)";
        if (v.s) {
          const on = v.s.on.bg || v.s.on.border, off = v.s.off.bg;
          for (const [color, label] of [[on, "selected"], [off, "not selected"]]) {
            if (!color || (label === "not selected" && !off)) continue;
            const sw = document.createElement("div"); sw.className = "swatch " + (label === "selected" ? "on" : "off");
            sw.style.background = color; sw.title = describe(v.s); d.append(sw);
          }
        }
      }
      if (!same(cur[k] || null, orig(k))) d.classList.add("dirty");
      if (sel.has(k)) d.classList.add("sel");
      d.onclick = () => { sel.has(k) ? sel.delete(k) : sel.add(k); render(); };
    }
    g.append(d);
  }
  const counts = {}; for (const v of Object.values(cur)) counts[v.g] = (counts[v.g] || 0) + 1;
  const lg = $("legend"); lg.replaceChildren();
  for (const l of Object.keys(counts).sort()) {
    const s = document.createElement("span"); s.textContent = l + ": " + counts[l] + " key" + (counts[l] === 1 ? "" : "s");
    s.style.background = "hsl(" + hue(l) + " 55% 30%)"; lg.append(s);
  }
  const locked = view.blocked;
  for (const id of ["assign", "clear", "review", "undo"]) $(id).disabled = locked;
  $("undo").disabled = locked || view.backups === 0;
}

async function load() {
  const n = parseInt($("page").value, 10);
  if (!(n >= 1)) { say("Enter a page number.", true); return; }
  try {
    view = await api("/api/page/" + n);
  } catch (e) { say(e.message, true); return; }
  localStorage.setItem("radioPage", String(n));
  cur = {}; sel.clear();
  for (const c of view.cells) if (c.group) cur[c.row + ":" + c.col] = {g: c.group, s: c.style};
  $("pname").textContent = "“" + view.name + "”";
  const w = $("warn"); w.hidden = !view.blocked;
  w.textContent = view.blocked ? "Page " + view.page + " is protected: you can look at it but it cannot be changed here." : "";
  $("plan").hidden = true; render();
}

$("load").onclick = load;
$("page").onkeydown = e => { if (e.key === "Enter") load(); };
$("none").onclick = () => { sel.clear(); if (view) render(); };
$("assign").onclick = () => {
  const l = $("label").value.trim();
  if (!/^[A-Za-z0-9_]+$/.test(l)) { say("Group label: use letters, digits or underscore only.", true); return; }
  if (!sel.size) { say("Click some keys first.", true); return; }
  const style = readStyle();
  if (!style.on.bg && !style.on.text && !style.on.border) { say("Choose at least one change for the selected look (background, text or border).", true); return; }
  for (const k of sel) cur[k] = {g: l, s: style};
  sel.clear(); say("", false); render();
};
$("clear").onclick = () => { for (const k of sel) delete cur[k]; sel.clear(); render(); };

$("review").onclick = async () => {
  const plan = $("plan"); plan.replaceChildren();
  try {
    const r = await api("/api/sync", {page: view.page, assignments: payload(), dry_run: true});
    const s = r.summary;
    if (!(s.added + s.changed + s.removed)) { say("Nothing to change.", false); plan.hidden = true; return; }
    const p = document.createElement("p");
    p.textContent = "Page " + view.page + " “" + view.name + "” will be replaced with: " + s.added + " key(s) added, " +
      s.changed + " moved to another group or restyled, " + s.removed + " removed from groups, " + s.unchanged + " unchanged. " +
      "A backup is taken first. Anything pressing keys on this page may be briefly interrupted.";
    const ok = document.createElement("button"); ok.className = "primary"; ok.textContent = "Apply now";
    const no = document.createElement("button"); no.textContent = "Cancel";
    no.onclick = () => { plan.hidden = true; };
    ok.onclick = async () => {
      ok.disabled = true; say("Working…", false);
      try {
        const res = await api("/api/sync", {page: view.page, assignments: payload(), dry_run: false});
        say(res.message, !res.ok);
      } catch (e) { say(e.message, true); }
      plan.hidden = true; await load();
    };
    plan.append(p, ok, " ", no); plan.hidden = false; say("", false);
  } catch (e) { say(e.message, true); }
};

$("undo").onclick = async () => {
  if (!confirm("Restore the most recent backup of page " + view.page + "?")) return;
  say("Working…", false);
  try { const r = await api("/api/restore", {page: view.page}); say(r.message, !r.ok); }
  catch (e) { say(e.message, true); }
  await load();
};
$("stop").onclick = async () => {
  if (!confirm("Stop Radio Builder? Press the key on the utility page to start it again.")) return;
  try { await api("/api/stop", {}); say("Stopped.", false); } catch (e) { say(e.message, true); }
};

$("page").value = localStorage.getItem("radioPage") || "1";
showColours();
load();
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
