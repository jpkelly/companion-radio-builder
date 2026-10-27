#!/usr/bin/env python3
"""Offline tests for radio-ui.py. Run: python3 -I radio-ui.test.py"""
import contextlib
import copy
import http.client
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("radio_ui", ROOT / "radio-ui.py")
ui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ui)
radio, rp = ui.radio, ui.rp

FIXTURE = ROOT / "fixtures" / "page16.json"


def regenerate_ids(o):
    if isinstance(o, dict):
        return {k: (rp.new_id() if k in ("id", "overrideId") and isinstance(v, str) and len(v) == 21 else regenerate_ids(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [regenerate_ids(x) for x in o]
    return o


class UiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["RADIO_BACKUP_DIR"] = self.tmp.name
        os.environ.pop("RADIO_BLOCKED_PAGES", None)
        os.environ.pop("RADIO_ALLOWED_PAGES", None)
        page = json.loads(FIXTURE.read_text())
        page["instances"] = {}
        self.pages = {19: page, 99: copy.deepcopy(page)}
        self.original = copy.deepcopy(page)
        self.imports = []
        radio.fetch_page = lambda host, n: copy.deepcopy(self.pages[n])
        radio.import_page = self.fake_import
        radio.fetch_connection_ids = lambda host: set()
        self.real_fetch_text = ui.fetch_text
        ui.fetch_text = lambda companion, page, row, col: None  # no Companion to ask: fall back to the template
        self.srv = ui.Server(("127.0.0.1", 0), "fake:1", idle_seconds=3600)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        ui.fetch_text = self.real_fetch_text
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()
        os.environ.pop("RADIO_BACKUP_DIR", None)

    def fake_import(self, host, new, target, remap):
        self.imports.append(target)
        self.pages[target] = regenerate_ids(copy.deepcopy(new))

    def req(self, method, path, body=None, headers=None, defaults=True):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            if defaults:
                h.setdefault("Content-Type", "application/json")
                h.setdefault("X-Radio-UI", "1")
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, raw

    def post(self, path, body, **kw):
        status, raw = self.req("POST", path, body, kw.get("headers"))
        return status, json.loads(raw)

    # ---------------------------------------------------------------- Companion address and version

    def test_companion_addresses_are_normalised(self):
        cases = {"192.168.1.20": "192.168.1.20:8000", " companion.local:8001 ": "companion.local:8001",
                 "http://10.0.0.5:8000/buttons": "10.0.0.5:8000"}
        for text, want in cases.items():
            self.assertEqual(ui.normalise_address(text), want, text)
        for text in ("", None, "a b", "host:0", "host:70000", "host:port"):
            self.assertIsNone(ui.normalise_address(text), text)

    def test_connect_checks_companion_then_uses_and_remembers_the_address(self):
        self.pages[1] = copy.deepcopy(self.original)
        status, r = self.post("/api/companion", {"address": "10.0.0.5"})
        self.assertEqual((status, r["companion"], r["warning"]), (200, "10.0.0.5:8000", None))
        status, raw = self.req("GET", "/api/status")
        self.assertEqual(json.loads(raw)["companion"], "10.0.0.5:8000")
        self.assertEqual(ui.startup_companion(None), ("10.0.0.5:8000", False))

    def test_connect_refuses_bad_or_unreachable_addresses_and_keeps_the_old_one(self):
        status, r = self.post("/api/companion", {"address": "not an address"})
        self.assertEqual(status, 400)

        def unreachable(host, n):
            raise radio.RadioError(f"cannot read page {n} from {host}: refused")

        radio.fetch_page = unreachable
        status, r = self.post("/api/companion", {"address": "10.9.9.9"})
        self.assertEqual(status, 400)
        self.assertIn("cannot read page 1 from 10.9.9.9:8000", r["error"])
        self.assertEqual(self.srv.companion, "fake:1")
        self.assertEqual(ui.startup_companion(None), (ui.DEFAULT_COMPANION, False))

    def test_an_address_given_at_start_is_fixed(self):
        self.assertEqual(ui.startup_companion("10.0.0.7"), ("10.0.0.7:8000", True))
        self.srv.locked = True
        self.pages[1] = copy.deepcopy(self.original)
        status, r = self.post("/api/companion", {"address": "10.0.0.5"})
        self.assertEqual(status, 400)
        self.assertEqual(self.srv.companion, "fake:1")
        status, raw = self.req("GET", "/api/status")
        self.assertTrue(json.loads(raw)["locked"])

    def test_companion_older_than_5_is_refused_and_untested_versions_warn(self):
        self.pages[19]["companionBuild"] = "4.1.0+123"
        status, raw = self.req("GET", "/api/page/19")
        self.assertEqual(status, 400)
        self.assertIn(b"needs Companion 5", raw)
        for build, warns in (("5.1.3+9", True), ("6.0.0", True), ("5.2.0+1", False)):
            self.pages[19]["companionBuild"] = build
            status, raw = self.req("GET", "/api/page/19")
            self.assertEqual(status, 200, build)
            self.assertEqual(bool(json.loads(raw)["warning"]), warns, build)

    def test_page_has_the_companion_field_and_connects_from_it(self):
        status, raw = self.req("GET", "/")
        page = raw.decode()
        self.assertIn('id="companion"', page)
        self.assertIn('api("/api/companion"', page)
        self.assertIn("status().then(load)", page)

    def test_start_on_a_busy_port_says_so_and_can_open_the_running_copy(self):
        opened, real_open = [], ui.webbrowser.open
        ui.webbrowser.open = opened.append
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                self.assertEqual(ui.main(["--bind", "127.0.0.1", "--port", str(self.port)]), 1)
                self.assertEqual(ui.main(["--bind", "127.0.0.1", "--port", str(self.port), "--open"]), 0)
        finally:
            ui.webbrowser.open = real_open
        self.assertIn("already in use", err.getvalue())
        self.assertEqual(opened, [f"http://localhost:{self.port}/"])

    # ---------------------------------------------------------------- read side

    def test_serves_the_page_and_never_uses_innerhtml(self):
        status, raw = self.req("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Radio Builder", raw)
        self.assertNotIn(b"innerHTML", raw, "page-derived text must only be set with textContent")

    def test_page_view_lists_cells_groups_and_protection(self):
        self.pages[19], _ = rp.sync_radio(self.pages[19], {("0", "1"): "A", ("0", "2"): "A"})
        status, raw = self.req("GET", "/api/page/19")
        v = json.loads(raw)
        self.assertEqual(status, 200)
        groups = {(c["row"], c["col"]): c["group"] for c in v["cells"] if c["kind"] == "button"}
        self.assertEqual(groups[("0", "1")], "A")
        self.assertIsNone(groups[("1", "1")])
        self.assertEqual({c["text"] for c in v["cells"] if c["kind"] == "other"}, {"pageup", "pagenum", "pagedown"})
        self.assertFalse(v["blocked"])
        self.assertTrue(json.loads(self.req("GET", "/api/page/99")[1])["blocked"])

    def test_works_for_a_service_user_without_a_home_directory(self):
        # regression: Path.home() used to be evaluated even when RADIO_BACKUP_DIR was set
        def no_home(cls):
            raise RuntimeError("Could not determine home directory.")

        original = Path.home
        Path.home = classmethod(no_home)
        try:
            status, raw = self.req("GET", "/api/page/19")
            self.assertEqual(status, 200, raw)
            status, r = self.post("/api/sync", {"page": 19, "assignments": {"0:1": "A"}})
            self.assertEqual(status, 200, r)
            self.assertTrue(r["ok"])
        finally:
            Path.home = original

    def test_unexpected_errors_get_a_response_not_a_dropped_connection(self):
        radio.fetch_page = lambda host, n: (_ for _ in ()).throw(ValueError("surprise"))
        status, raw = self.req("GET", "/api/page/19")
        self.assertEqual(status, 500)
        self.assertFalse(json.loads(raw)["ok"])

    def test_per_key_looks_round_trip_and_restyle_through_the_api(self):
        gold = {"on": {"bg": None, "text": None, "border": "#ffcc00"}, "off": {"bg": None, "text": None}}
        green = {"on": {"bg": "#003300", "text": "auto", "border": None}, "off": {"bg": "#202020", "text": "auto"}}
        want = {"0:1": {"group": "A", "style": gold}, "0:2": {"group": "A", "style": green}}
        status, r = self.post("/api/sync", {"page": 19, "assignments": want})
        self.assertTrue(r["ok"], r)
        cells = {(c["row"], c["col"]): c for c in json.loads(self.req("GET", "/api/page/19")[1])["cells"]}
        self.assertEqual(cells[("0", "1")]["style"], gold)
        # auto text was resolved to concrete colours when written, and is reported as such
        self.assertEqual(cells[("0", "2")]["style"], {"on": {"bg": "#003300", "text": "#ffffff", "border": None},
                                                       "off": {"bg": "#202020", "text": "#ffffff"}})
        self.assertIsNone(cells[("1", "1")]["style"])
        # asking for the same looks again changes nothing, even though "auto" was resolved on the page
        status, r = self.post("/api/sync", {"page": 19, "assignments": want, "dry_run": True})
        self.assertEqual(r["summary"], {"added": 0, "changed": 0, "removed": 0, "unchanged": 2})
        # restyle one key only: the other counts as unchanged
        want["0:2"]["style"] = {"on": {"bg": "#cc0000", "text": None, "border": "#ffffff"}, "off": {"bg": None, "text": None}}
        status, r = self.post("/api/sync", {"page": 19, "assignments": want, "dry_run": True})
        self.assertEqual(r["summary"], {"added": 0, "changed": 1, "removed": 0, "unchanged": 1})
        status, r = self.post("/api/sync", {"page": 19, "assignments": want})
        self.assertTrue(r["ok"], r)
        cells = {(c["row"], c["col"]): c for c in json.loads(self.req("GET", "/api/page/19")[1])["cells"]}
        self.assertEqual(cells[("0", "2")]["style"], want["0:2"]["style"])
        self.assertEqual(cells[("0", "1")]["style"], gold)
        # a null style keeps what is there
        status, r = self.post("/api/sync", {"page": 19, "assignments": {k: {"group": v["group"], "style": None} for k, v in want.items()}})
        self.assertEqual(r["message"], "Nothing to change.")

    def test_bad_looks_are_refused(self):
        nothing_on = {"on": {"bg": None, "text": None, "border": None}, "off": {"bg": "#202020", "text": None}}
        for style in (nothing_on, {"on": {"bg": "red"}}, {"on": {"bg": "auto"}}, {"mode": "fill", "color": "#ffcc00"}, "border", {"on": "x"}):
            status, r = self.post("/api/sync", {"page": 19, "assignments": {"0:1": {"group": "A", "style": style}}})
            self.assertEqual(status, 400, style)
        self.assertEqual(self.imports, [])

    def test_page_has_the_look_controls(self):
        page = self.req("GET", "/")[1]
        for f in ("on_bg", "on_text", "on_border", "off_bg", "off_text"):
            self.assertIn(f'id="{f}_m"'.encode(), page)
            self.assertIn(f'id="{f}_c"'.encode(), page)
        self.assertIn(b"Auto (readable)", page)

    def set_label(self, template):
        for row in self.pages[19]["page"]["controls"].values():
            for b in row.values():
                if b["type"] == "button-layered":
                    for l in b["style"]["layers"]:
                        if l["type"] == "text":
                            l["text"] = {"isExpression": False, "value": template}

    def cells(self):
        return {(c["row"], c["col"]): c for c in json.loads(self.req("GET", "/api/page/19")[1])["cells"]}

    def test_labels_show_what_companion_renders_and_keep_the_template_as_a_tooltip(self):
        self.set_label("$(this:page)$(this:row)$(this:column)")
        asked = []

        def rendered(companion, page, row, col):
            asked.append((companion, page, row, col))
            return f"{page}{row}{col}"

        ui.fetch_text = rendered
        cells = self.cells()
        self.assertEqual(cells[("0", "1")]["text"], "1901")
        self.assertEqual(cells[("0", "1")]["template"], "$(this:page)$(this:row)$(this:column)")
        self.assertEqual(cells[("2", "3")]["text"], "1923")
        self.assertEqual(len(asked), len([c for c in cells.values() if c["kind"] == "button"]), "one lookup per button")
        self.assertTrue(all(a[0] == "fake:1" and a[1] == 19 for a in asked))
        self.assertNotIn("template", cells[("0", "0")], "navigation keys have no label template")

    def test_labels_fall_back_to_the_template_when_companion_cannot_answer(self):
        self.set_label("$(this:page)$(this:row)$(this:column)")
        ui.fetch_text = lambda companion, page, row, col: None if (row, col) == ("0", "1") else "ok"
        cells = self.cells()
        self.assertEqual(cells[("0", "1")]["text"], "$(this:page)$(this:row)$(this:column)")
        self.assertEqual(cells[("0", "2")]["text"], "ok")

    def test_multiline_and_long_rendered_text_is_tidied(self):
        ui.fetch_text = lambda companion, page, row, col: "RADIO UI\n  host\n.local:8088 " + "x" * 80
        text = self.cells()[("0", "1")]["text"]
        self.assertTrue(text.startswith("RADIO UI host .local:8088 xxx"))
        self.assertLessEqual(len(text), 40)

    def test_literal_backslash_n_line_break_markers_read_as_spaces(self):
        # Companion returns a label written with "\n" as the two characters backslash and n
        ui.fetch_text = lambda companion, page, row, col: "10.0.0.157\\n10.42.0.1"
        self.assertEqual(self.cells()[("0", "1")]["text"], "10.0.0.157 10.42.0.1")

    def test_an_empty_rendered_label_stays_empty(self):
        ui.fetch_text = lambda companion, page, row, col: ""
        self.assertEqual(self.cells()[("0", "1")]["text"], "")

    def test_fetch_text_reads_the_variable_endpoint_and_survives_failures(self):
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                if self.path.endswith("/b_text_19_0_1/value"):
                    body = b"1901"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_error(404)

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            host = f"127.0.0.1:{srv.server_address[1]}"
            self.assertEqual(self.real_fetch_text(host, 19, "0", "1"), "1901")
            self.assertEqual(seen, ["/api/variable/internal/b_text_19_0_1/value"])
            self.assertIsNone(self.real_fetch_text(host, 19, "9", "9"), "404 means unavailable")
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertIsNone(self.real_fetch_text("127.0.0.1:1", 19, "0", "1"), "nothing listening means unavailable")

    def test_page_script_shows_the_template_tooltip(self):
        self.assertIn(b"Label template: ", self.req("GET", "/")[1])

    def test_unreadable_page_is_a_clean_error(self):
        radio.fetch_page = lambda host, n: (_ for _ in ()).throw(radio.RadioError("cannot read page 5"))
        status, raw = self.req("GET", "/api/page/5")
        self.assertEqual(status, 400)
        self.assertIn("cannot read page 5", json.loads(raw)["error"])

    # ---------------------------------------------------------------- changes

    def test_review_then_apply_then_undo(self):
        want = {"0:1": "A", "0:2": "A", "2:1": "B"}
        status, r = self.post("/api/sync", {"page": 19, "assignments": want, "dry_run": True})
        self.assertEqual((status, r["summary"]["added"]), (200, 3))
        self.assertEqual(self.imports, [], "a review must not import")
        status, r = self.post("/api/sync", {"page": 19, "assignments": want})
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.imports, [19])
        self.assertEqual(rp.list_radio(self.pages[19]), {"A": [("0", "1"), ("0", "2")], "B": [("2", "1")]})
        self.assertEqual(json.loads(self.req("GET", "/api/page/19")[1])["backups"], 1)
        status, r = self.post("/api/sync", {"page": 19, "assignments": want})
        self.assertEqual(r["message"], "Nothing to change.")
        self.assertEqual(self.imports, [19], "no change means no import")
        status, r = self.post("/api/restore", {"page": 19})
        self.assertTrue(r["ok"], r)
        self.assertTrue(rp.same_ignoring_ids(self.pages[19], self.original))
        self.assertEqual(len(radio.list_backups(19)), 1, "undo's own safety backup is not offered as an undo target")

    def test_protected_page_cannot_be_changed(self):
        status, r = self.post("/api/sync", {"page": 99, "assignments": {"0:1": "A"}})
        self.assertEqual(status, 400)
        self.assertIn("protected", r["error"])
        status, r = self.post("/api/restore", {"page": 99})
        self.assertEqual(status, 400)
        self.assertEqual(self.imports, [])

    def test_bad_requests_are_refused_without_importing(self):
        cases = [
            {"page": "19", "assignments": {}},                      # page not an int
            {"page": 19, "assignments": []},                        # assignments not an object
            {"page": 19, "assignments": {"0-1": "A"}},              # bad key
            {"page": 19, "assignments": {"0:1": "A B"}},            # bad label
            {"page": 19, "assignments": {"0:0": "A"}},              # navigation key
            {"page": 19, "assignments": {"7:7": "A"}},              # no such key
        ]
        for body in cases:
            status, r = self.post("/api/sync", body)
            self.assertEqual(status, 400, body)
            self.assertFalse(r["ok"])
        self.assertEqual(self.imports, [])

    def test_restore_rejects_foreign_or_traversal_names(self):
        for name in ("../../etc/passwd", "20990101-000000-000-page19-before.json", "x.json"):
            status, r = self.post("/api/restore", {"page": 19, "name": name})
            self.assertEqual(status, 400, name)
        self.assertEqual(self.imports, [])

    def test_changes_are_refused_while_another_is_running(self):
        self.srv.op_lock.acquire()
        try:
            status, r = self.post("/api/sync", {"page": 19, "assignments": {"0:1": "A"}})
            self.assertEqual(status, 409)
        finally:
            self.srv.op_lock.release()

    # ---------------------------------------------------------------- web safety

    def test_post_needs_the_custom_header_json_and_same_origin(self):
        body = json.dumps({"page": 19, "assignments": {"0:1": "A"}}).encode()
        for headers in (
            {"Content-Type": "application/json"},                                   # no X-Radio-UI
            {"X-Radio-UI": "1", "Content-Type": "text/plain"},                      # not JSON
            {"X-Radio-UI": "1", "Content-Type": "application/json", "Origin": "http://evil.example"},
        ):
            status, raw = self.req("POST", "/api/sync", body, headers, defaults=False)
            self.assertEqual(status, 403, headers)
        self.assertEqual(self.imports, [])
        same = {"X-Radio-UI": "1", "Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.req("POST", "/api/sync", body, same, defaults=False)[0], 200)

    def test_garbage_body_and_unknown_paths(self):
        self.assertEqual(self.req("POST", "/api/sync", b"{not json")[0], 400)
        self.assertEqual(self.req("POST", "/api/nope", {"page": 19})[0], 404)
        self.assertEqual(self.req("GET", "/api/nope")[0], 404)

    # ---------------------------------------------------------------- lifecycle

    def test_stop_endpoint_stops_the_server(self):
        self.assertEqual(self.post("/api/stop", {})[0], 200)
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def test_idle_timeout_stops_the_server(self):
        self.srv.idle_seconds = 0.3
        threading.Thread(target=ui.watch_idle, args=(self.srv, 0.1), daemon=True).start()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive(), "server should stop itself when idle")

    def test_idle_timeout_waits_for_a_running_change(self):
        self.srv.idle_seconds = 0.2
        self.srv.op_lock.acquire()
        threading.Thread(target=ui.watch_idle, args=(self.srv, 0.1), daemon=True).start()
        time.sleep(0.8)
        self.assertTrue(self.thread.is_alive(), "must not stop in the middle of a change")
        self.srv.op_lock.release()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    # ---------------------------------------------------------------- front-end script

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_embedded_javascript_is_syntactically_valid(self):
        script = re.search(r"<script>(.*)</script>", ui.PAGE_HTML, re.S).group(1)
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "ui.js"
            p.write_text(script)
            out = subprocess.run(["node", "--check", str(p)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
