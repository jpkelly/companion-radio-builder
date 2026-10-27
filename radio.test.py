#!/usr/bin/env python3
"""Offline tests for radio.py. Run: python3 -I radio.test.py"""
import contextlib
import copy
import gzip
import http.server
import importlib.util
import io
import json
import os
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("radio", ROOT / "radio.py")
radio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(radio)
rp = radio.rp
REAL_FETCH_PAGE = radio.fetch_page  # WorkflowTests replace it with a fake

FIXTURE = ROOT / "fixtures" / "page16.json"


# ---------------------------------------------------------------- a tiny fake Companion websocket server

def _recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError
        buf += chunk
    return buf


def server_read_frame(sock):
    b1, b2 = _recv_exact(sock, 2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    assert b2 & 0x80, "client frames must be masked"
    mask = _recv_exact(sock, 4)
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(_recv_exact(sock, n)))
    return b1 & 0x0F, payload


def server_frame(opcode, payload, fin=True):
    n = len(payload)
    hdr = bytearray([(0x80 if fin else 0) | opcode])
    if n < 126:
        hdr.append(n)
    elif n < 65536:
        hdr.append(126)
        hdr += struct.pack(">H", n)
    else:
        hdr.append(127)
        hdr += struct.pack(">Q", n)
    return bytes(hdr) + payload


class FakeCompanion(threading.Thread):
    def __init__(self, refuse=False):
        super().__init__(daemon=True)
        self.refuse = refuse
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.pong_seen = False
        self.start()

    def run(self):
        conn, _ = self.srv.accept()
        try:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(4096)
            if self.refuse:
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
                return
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
            while True:
                op, payload = server_read_frame(conn)
                while op == 10:  # a pong to our ping: control frames carry no request
                    op, payload = server_read_frame(conn)
                req = json.loads(payload)
                conn.sendall(server_frame(1, b"PING"))  # keep-alive the client must answer
                op2, pong = server_read_frame(conn)
                self.pong_seen = (op2 == 1 and pong == b"PONG")
                path = req["params"]["path"]
                if path == "t.err":
                    conn.sendall(server_frame(1, json.dumps({"id": req["id"], "error": {"message": "boom"}}).encode()))
                elif path == "t.big":
                    big = "x" * 70000
                    body = json.dumps({"id": req["id"], "result": {"type": "data", "data": {"got": len(req["params"]["input"]["data"]), "echo": big}}}).encode()
                    a, b, c = body[:20000], body[20000:50000], body[50000:]
                    conn.sendall(server_frame(1, a, fin=False))
                    conn.sendall(server_frame(9, b"hi"))  # ping in the middle of a fragmented message
                    conn.sendall(server_frame(0, b, fin=False))
                    conn.sendall(server_frame(0, c, fin=True))
                else:
                    conn.sendall(server_frame(1, json.dumps({"id": req["id"], "result": {"type": "data", "data": "ok"}}).encode()))
        except (EOFError, OSError):
            pass
        finally:
            conn.close()
            self.srv.close()


class WebsocketClientTests(unittest.TestCase):
    def test_calls_keepalive_fragments_and_large_frames(self):
        fake = FakeCompanion()
        t = radio.Trpc(f"127.0.0.1:{fake.port}")
        try:
            self.assertEqual(t.call("t.ok", {"a": 1}), "ok")
            self.assertTrue(fake.pong_seen, "client must answer PING with PONG")
            out = t.call("t.big", {"data": "y" * 200000})  # >64 KiB request uses the 8-byte length
            self.assertEqual(out["got"], 200000)
            self.assertEqual(len(out["echo"]), 70000)  # fragmented, with a ping in the middle
            with self.assertRaisesRegex(radio.RadioError, "boom"):
                t.call("t.err")
        finally:
            t.close()

    def test_refused_upgrade_is_reported(self):
        fake = FakeCompanion(refuse=True)
        with self.assertRaisesRegex(radio.RadioError, "403"):
            radio.Trpc(f"127.0.0.1:{fake.port}")


# ---------------------------------------------------------------- workflow against an in-memory Companion

def regenerate_ids(o):
    if isinstance(o, dict):
        return {k: (rp.new_id() if k in ("id", "overrideId") and isinstance(v, str) and len(v) == 21 else regenerate_ids(v)) for k, v in o.items()}
    if isinstance(o, list):
        return [regenerate_ids(x) for x in o]
    return o


class VersionTests(unittest.TestCase):
    def test_tested_version_passes_others_warn_and_older_than_5_is_refused(self):
        self.assertIsNone(radio.check_version({"companionBuild": "5.2.0+10027-main-eee648c42b"}))
        self.assertIsNone(radio.check_version({"companionBuild": "5.2.3"}))
        self.assertIn("5.1.0", radio.check_version({"companionBuild": "5.1.0+1"}))
        self.assertIn("6.0.0", radio.check_version({"companionBuild": "6.0.0"}))
        self.assertIn("Could not tell", radio.check_version({}))
        with self.assertRaisesRegex(radio.RadioError, "needs Companion 5"):
            radio.check_version({"companionBuild": "4.1.2+3"})

    def test_fetch_page_refuses_old_companions_and_other_web_servers(self):
        answer = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(answer["body"])))
                self.end_headers()
                self.wfile.write(answer["body"])

            def log_message(self, *args):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        host = f"127.0.0.1:{srv.server_address[1]}"
        try:
            answer["body"] = json.dumps({"type": "page", "companionBuild": "4.1.0", "page": {}}).encode()
            with self.assertRaisesRegex(radio.RadioError, "needs Companion 5"):
                REAL_FETCH_PAGE(host, 1)
            answer["body"] = b"<html>router login</html>"
            with self.assertRaisesRegex(radio.RadioError, "did not answer like Companion"):
                REAL_FETCH_PAGE(host, 1)
            answer["body"] = gzip.compress(FIXTURE.read_bytes())
            self.assertEqual(REAL_FETCH_PAGE(host, 16)["page"]["name"], "BG RAD")
        finally:
            srv.shutdown()
            srv.server_close()


class Fake:
    """Stands in for a live Companion: fetch_page / import_page act on one page."""

    def __init__(self, page):
        self.state = page
        self.imports = []
        self.fetches = 0
        self.mutate_on_fetch = None  # callable(n) -> None, to simulate concurrent edits
        self.drop_button_on_import = False

    def fetch(self, host, page):
        self.fetches += 1
        if self.mutate_on_fetch:
            self.mutate_on_fetch(self.fetches, self.state)
        return copy.deepcopy(self.state)

    def do_import(self, host, new, target, remap):
        self.imports.append((target, remap))
        new = regenerate_ids(copy.deepcopy(new))  # Companion regenerates entity ids
        if self.drop_button_on_import:
            new["page"]["controls"]["0"].pop("1")
        self.state = new


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["RADIO_BACKUP_DIR"] = self.tmp.name
        os.environ.pop("RADIO_ALLOWED_PAGES", None)
        self.fake = Fake(json.loads(FIXTURE.read_text()))
        self.fake.state["instances"] = {}
        radio.fetch_page = self.fake.fetch
        radio.import_page = self.fake.do_import
        self.known_connections = set()  # connections that exist anywhere on the fake Companion
        radio.fetch_connection_ids = lambda host: set(self.known_connections)
        self.original = copy.deepcopy(self.fake.state)

    def tearDown(self):
        self.tmp.cleanup()
        os.environ.pop("RADIO_BACKUP_DIR", None)
        os.environ.pop("RADIO_ALLOWED_PAGES", None)

    def run_cmd(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = radio.main(["--host", "x:1", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_apply_then_strip_round_trip(self):
        code, out, _ = self.run_cmd("apply", "19", "-g", "A=0:1,0:2", "-g", "B=2:1", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("VERIFIED", out)
        self.assertEqual(self.fake.imports[0][0], 19)
        self.assertEqual(rp.list_radio(self.fake.state), {"A": [("0", "1"), ("0", "2")], "B": [("2", "1")]})
        backups = list(Path(self.tmp.name).glob("*-page19-before.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), self.original, "backup is the pre-change page")
        code, out, _ = self.run_cmd("strip", "19", "--yes")
        self.assertEqual(code, 0, out)
        self.assertTrue(rp.same_ignoring_ids(self.fake.state, self.original))

    def test_old_companion_is_refused_and_untested_versions_warn(self):
        self.fake.state["companionBuild"] = "4.1.0"
        code, _, err = self.run_cmd("list", "19")
        self.assertEqual(code, 2)
        self.assertIn("needs Companion 5", err)
        self.assertEqual(self.fake.imports, [])
        self.fake.state["companionBuild"] = "5.1.0"
        code, out, err = self.run_cmd("apply", "19", "-g", "A=0:1", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("warning: This is Companion 5.1.0", err)

    def test_dry_run_imports_nothing(self):
        code, out, _ = self.run_cmd("apply", "19", "-g", "A=0:1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.imports, [])
        self.assertEqual(len(list(Path(self.tmp.name).glob("*-proposed.json"))), 1)

    def test_declining_the_prompt_changes_nothing(self):
        radio.confirm.__globals__["input"] = lambda prompt="": "n"
        radio.sys.stdin = type("T", (), {"isatty": lambda self: True})()
        try:
            code, out, _ = self.run_cmd("apply", "19", "-g", "A=0:1")
        finally:
            del radio.confirm.__globals__["input"]
        self.assertEqual(code, 0)
        self.assertIn("cancelled", out)
        self.assertEqual(self.fake.imports, [])

    def test_no_terminal_and_no_yes_refuses(self):
        radio.sys.stdin = type("T", (), {"isatty": lambda self: False})()
        code, _, err = self.run_cmd("apply", "19", "-g", "A=0:1")
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        self.assertEqual(self.fake.imports, [])

    def test_page_edited_while_working_is_not_overwritten(self):
        def edit(n, state):
            if n == 2:  # the re-check read just before importing
                state["page"]["name"] = "someone renamed me"

        self.fake.mutate_on_fetch = edit
        code, _, err = self.run_cmd("apply", "19", "-g", "A=0:1", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("edited while we were working", err)
        self.assertEqual(self.fake.imports, [])

    def test_allowed_pages_restriction(self):
        os.environ["RADIO_ALLOWED_PAGES"] = "19,20"
        code, _, err = self.run_cmd("apply", "12", "-g", "A=0:1", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("RADIO_ALLOWED_PAGES", err)
        self.assertEqual(self.fake.imports, [])

    def test_connections_missing_on_live_page_refuse(self):
        self.fake.state["instances"] = {"conn1": {"label": "x"}}

        def drop(n, state):
            if n == 2:
                state["instances"] = {}

        self.fake.mutate_on_fetch = drop
        code, _, err = self.run_cmd("apply", "19", "-g", "A=0:1", "--yes")
        self.assertEqual(code, 2)
        self.assertIn("conn1", err)
        self.assertEqual(self.fake.imports, [])

    def test_connection_unused_on_the_target_page_but_present_on_the_system_is_accepted(self):
        # e.g. copying a saved page onto an empty page: the target does not list the connection yet
        self.fake.state["instances"] = {"conn1": {"label": "x"}}

        def drop(n, state):
            if n == 2:
                state["instances"] = {}

        self.fake.mutate_on_fetch = drop
        self.known_connections = {"conn1"}
        code, out, err = self.run_cmd("apply", "19", "-g", "A=0:1", "--yes")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.fake.imports[0][1], {"conn1": "conn1"})

    def test_connections_are_mapped_to_themselves(self):
        self.fake.state["instances"] = {"conn1": {"label": "x"}, "conn2": {"label": "y"}}
        code, _, _ = self.run_cmd("apply", "19", "-g", "A=0:1", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.imports[0][1], {"conn1": "conn1", "conn2": "conn2"})

    def test_mismatch_is_reported_with_restore_hint(self):
        self.fake.drop_button_on_import = True
        code, out, _ = self.run_cmd("apply", "19", "-g", "A=0:1,0:2", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("MISMATCH", out)
        self.assertIn("radio load", out)

    def test_load_restores_a_backup(self):
        self.run_cmd("apply", "19", "-g", "A=0:1", "--yes")
        backup = next(Path(self.tmp.name).glob("*-page19-before.json"))
        code, out, _ = self.run_cmd("load", str(backup), "19", "--yes")
        self.assertEqual(code, 0, out)
        self.assertTrue(rp.same_ignoring_ids(self.fake.state, self.original))

    def test_strip_with_nothing_to_strip(self):
        code, out, _ = self.run_cmd("strip", "19", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("nothing to do", out)
        self.assertEqual(self.fake.imports, [])

    def test_bad_group_spec_is_a_clean_error(self):
        code, _, err = self.run_cmd("apply", "19", "-g", "A B=0:1", "--yes")
        self.assertEqual(code, 2)
        self.assertEqual(self.fake.imports, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
