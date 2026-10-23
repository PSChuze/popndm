"""The setup program's helpers: the shared DNS resolver (docker calls faked), pairing with
an OpenLobby panel (against a local stand-in) and the server check (against a real gate
and rankings server in this process)."""
import http.server as httpserver
import json
import os
import shutil
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from hostsetup import dns, http, panel, smoke
from popn import gate, model, web, wire


class FakeDocker:
    """Answers docker commands from a table of (first words -> (exit code, output))."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, cmd, env=None, cwd=None):
        self.calls.append((cmd, env))
        for prefix, answer in self.answers:
            if cmd[:len(prefix)] == prefix:
                return answer
        raise AssertionError("unexpected command %r" % (cmd,))


class Dns(unittest.TestCase):
    def test_find(self):
        run = FakeDocker([(["docker", "ps"], (0, "openlobby-dns-1\n"))])
        self.assertEqual(dns.find(run), "openlobby-dns-1")
        self.assertIn("label=org.openlobby.role=dns", run.calls[0][0])
        self.assertIsNone(dns.find(FakeDocker([(["docker", "ps"], (0, ""))])))
        self.assertIsNone(dns.find(FakeDocker([(["docker", "ps"], (1, "Cannot connect"))])))

    def test_add_names(self):
        run = FakeDocker([(["docker", "exec"], (0, "popn: 2 name(s)"))])
        self.assertEqual(dns.add_names("r", run)[0], "ok")
        self.assertEqual(run.calls[0][0], ["docker", "exec", "r", "python", "stub.py", "dns-names", "add", "popn",
                                           "ps2pzd1.konamionline.com", "ps2pzdweb.konamionline.com"])

    def test_an_older_resolver(self):
        run = FakeDocker([(["docker", "exec"], (1, "usage: stub.py {dns|http|tcp}"))])
        self.assertEqual(dns.add_names("r", run)[0], "old")
        run = FakeDocker([(["docker", "exec"], (2, "dns-names: not a host name: x"))])
        self.assertEqual(dns.add_names("r", run)[0], "failed")

    def test_remove_and_ours(self):
        run = FakeDocker([(["docker", "exec"], (0, "removed the names of popn")),
                          (["docker", "inspect"], (0, "openlobby-dns\n"))])
        self.assertTrue(dns.remove_names("r", run)[0])
        self.assertEqual(run.calls[0][0][-2:], ["remove", "popn"])
        self.assertTrue(dns.ours("r", run))
        self.assertFalse(dns.ours("r", FakeDocker([(["docker", "inspect"], (0, "openlobby\n"))])))

    def test_start(self):
        run = FakeDocker([(["docker", "compose"], (0, ""))])
        ok, _ = dns.start("/srv/popn", dns.settings("home", "10.0.0.20", "", False), run, port=15353)
        self.assertTrue(ok)
        cmd, env = run.calls[0]
        self.assertEqual(cmd[:4], ["docker", "compose", "-p", "openlobby-dns"])
        self.assertTrue(cmd[5].endswith(os.path.join("dns", "compose.yml")))
        self.assertEqual((env["POL_ADVERTISE"], env["POL_DNS_PUBLIC"], env["OPENLOBBY_DNS_PORT"]),
                         ("10.0.0.20", "0", "15353"))

    def test_settings(self):
        self.assertEqual(dns.settings("internet", "10.0.0.20", "203.0.113.5", desktop=False),
                         {"POL_ADVERTISE": "10.0.0.20", "POL_ADVERTISE_PUBLIC": "203.0.113.5",
                          "POL_DNS_PUBLIC": "1"})
        self.assertEqual(dns.settings("internet", "10.0.0.20", "203.0.113.5", desktop=True)["POL_ADVERTISE"],
                         "203.0.113.5")

    def test_compose_file_matches(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "dns", "compose.yml"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("org.openlobby.role: dns", text)
        self.assertIn('command: ["dns"]', text)
        self.assertIn(":53/udp", text)
        self.assertIn("POL_ADVERTISE:", text)
        self.assertNotIn("/config", text)        # the image starts without a settings file


class Port80(unittest.TestCase):
    def test_find_and_add(self):
        run = FakeDocker([(["docker", "ps"], (0, "openlobby-http-1\n")),
                          (["docker", "exec"], (0, "popn: 1 host(s)"))])
        self.assertEqual(http.find(run), "openlobby-http-1")
        self.assertIn("label=org.openlobby.role=http", run.calls[0][0])
        self.assertEqual(http.add_hosts("openlobby-http-1", run)[0], "ok")
        self.assertEqual(run.calls[1][0], ["docker", "exec", "openlobby-http-1", "python", "stub.py", "http-hosts",
                                           "add", "popn", "http://popn-web:5732", "ps2pzdweb.konamionline.com"])

    def test_older_and_remove(self):
        run = FakeDocker([(["docker", "exec"], (1, "usage: stub.py {dns|http|tcp|dns-names}"))])
        self.assertEqual(http.add_hosts("h", run)[0], "old")
        run = FakeDocker([(["docker", "exec"], (0, "removed"))])
        self.assertTrue(http.remove_hosts("h", run)[0])
        self.assertEqual(run.calls[0][0][-2:], ["remove", "popn"])
        self.assertIsNone(http.find(FakeDocker([(["docker", "ps"], (0, ""))])))

    def test_start(self):
        run = FakeDocker([(["docker", "compose"], (0, ""))])
        self.assertEqual(http.start("/srv/popn", run, taken=lambda: False)[0], "ok")
        cmd = run.calls[0][0]
        self.assertEqual(cmd[:4], ["docker", "compose", "-p", "openlobby-http"])
        self.assertTrue(cmd[5].endswith(os.path.join("http", "compose.yml")))

    def test_port_80_held_by_another_program(self):
        run = FakeDocker([])
        self.assertEqual(http.start("/srv/popn", run, taken=lambda: True)[0], "busy")
        self.assertEqual(run.calls, [])
        run = FakeDocker([(["docker", "compose"], (1, "Bind for 0.0.0.0:80 failed: port is already allocated"))])
        self.assertEqual(http.start("/srv/popn", run, taken=lambda: False)[0], "busy")
        run = FakeDocker([(["docker", "compose"], (1, "pull access denied"))])
        self.assertEqual(http.start("/srv/popn", run, taken=lambda: False)[0], "failed")

    def test_compose_file(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "http", "compose.yml"), encoding="utf-8") as f:
            text = f.read()
        for want in ("org.openlobby.role: http", 'command: ["http"]', '"80:80"', "http-data:/data",
                     "external: true"):
            self.assertIn(want, text)

    def test_compose_alias(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "compose.yml"), encoding="utf-8") as f:
            self.assertIn("aliases: [popn-web]", f.read())


class Panel(httpserver.BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append((self.path, body))
        held = {"updater": "requested"} if "updater" in body else {}
        if self.path == "/api/pair" and body["code"] == "ABCD2345":
            self._send(200, dict(held, ok=True, title=body["title"], pairing="secret-1"))
        elif self.path == "/api/pair":
            self._send(403, {"error": "That code is wrong or has expired."})
        elif body.get("pairing") == "secret-1":
            self._send(200, dict(held, ok=True))
        else:
            self._send(403, {"error": "Wrong pairing."})

    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class Pairing(unittest.TestCase):
    def setUp(self):
        self.server = httpserver.HTTPServer(("127.0.0.1", 0), Panel)
        self.server.seen = []
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:%d" % self.server.server_port

    def test_pair_and_update(self):
        secret, msg = panel.pair(self.url, " ABCD2345 ", "popn", "pop'n Puzzle-dama Online", "http://popn:5738",
                                 "tok")
        self.assertEqual((secret, msg), ("secret-1", "Paired with the panel."))
        self.assertEqual(self.server.seen[0], ("/api/pair", {"code": "ABCD2345", "key": "popn",
                                                             "title": "pop'n Puzzle-dama Online",
                                                             "url": "http://popn:5738", "token": "tok"}))
        self.assertEqual(panel.update(self.url, "popn", "secret-1", "tok2"), (True, "The panel has the new settings."))
        self.assertEqual(self.server.seen[1], ("/api/pair/update", {"key": "popn", "pairing": "secret-1",
                                                                    "token": "tok2"}))

    def test_updater(self):
        updater = {"folder": "/srv/popn", "command": ["python", "setup.py", "--update", "--yes"],
                   "timeout_minutes": 60}
        secret, msg = panel.pair(self.url, "ABCD2345", "popn", "t", "http://popn:5738", "tok", updater=updater)
        self.assertEqual(secret, "secret-1")
        self.assertEqual(msg, "Paired with the panel. " + panel.REQUESTED)
        self.assertEqual(self.server.seen[0][1]["updater"], updater)
        ok, msg = panel.update(self.url, "popn", "secret-1", "tok", "http://popn:5738", updater=updater)
        self.assertTrue(ok)
        self.assertTrue(msg.endswith("on Settings > Modules."))
        self.assertEqual(self.server.seen[1][1]["updater"], updater)

    def test_refusals(self):
        self.assertEqual(panel.pair(self.url, "WRONG", "popn", "t", "u", "tok"),
                         (None, "That code is wrong or has expired."))
        self.assertEqual(panel.update(self.url, "popn", "nope", "tok"), (False, "Wrong pairing."))

    def test_no_panel_answering(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        secret, msg = panel.pair("http://127.0.0.1:%d" % port, "x", "popn", "t", "u", "tok")
        self.assertIsNone(secret)
        self.assertTrue(msg.startswith("The panel did not answer"))

    def test_find(self):
        self.assertEqual(panel.find(lambda cmd: (0, "openlobby-panel-1\thttp://127.0.0.1:8090/\n")),
                         "http://127.0.0.1:8090")
        self.assertIsNone(panel.find(lambda cmd: (0, "\n")))
        self.assertIsNone(panel.find(lambda cmd: (1, "openlobby-panel-1\thttp://127.0.0.1:8090\n")))

    def test_several_panels(self):
        two = lambda cmd: (0, "panel-a\thttp://127.0.0.1:8090/\npanel-b\thttp://127.0.0.1:8091\nno-url\t\n")
        self.assertEqual(panel.panels(two), [("panel-a", "http://127.0.0.1:8090"),
                                             ("panel-b", "http://127.0.0.1:8091")])
        self.assertIsNone(panel.find(two))            # never a guess
        self.assertEqual(panel.several(panel.panels(two)),
                         "Not paired: more than one OpenLobby admin panel is running here (panel-a at "
                         "http://127.0.0.1:8090, panel-b at http://127.0.0.1:8091); run pop'n Server Setup "
                         "and choose the panel.")


class Check(unittest.TestCase):
    """smoke.check against a real gate and rankings server."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        env = mock.patch.dict(os.environ, {"POPN_LOG_DIR": self.dir,
                                           "POPN_STATE_DB": os.path.join(self.dir, "accounts.json")})
        env.start()
        self.addCleanup(env.stop)
        model.init(None)
        srv = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(srv.close)
        self.gate_port = srv.getsockname()[1]

        def accept():
            while True:
                try:
                    sock, peer = srv.accept()
                except OSError:
                    return
                s = gate.Session(sock, peer, self.dir, advertise="192.0.2.10")
                threading.Thread(target=s.run, daemon=True).start()
        threading.Thread(target=accept, daemon=True).start()
        self.web = ThreadingHTTPServer(("127.0.0.1", 0), web.Handler)
        threading.Thread(target=self.web.serve_forever, daemon=True).start()
        self.addCleanup(self.web.server_close)
        self.addCleanup(self.web.shutdown)

    def test_check(self):
        self.assertEqual(smoke.server_list("127.0.0.1", self.gate_port), "192.0.2.10")
        self.assertTrue(smoke.ranking("127.0.0.1", self.web.server_port))
        ok, msg = smoke.check("127.0.0.1", self.gate_port, self.web.server_port)
        self.assertTrue(ok, msg)
        self.assertIn("192.0.2.10", msg)

    def test_nothing_listening(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        ok, msg = smoke.check("127.0.0.1", port, self.web.server_port)
        self.assertFalse(ok)
        self.assertIn("5731", msg)

    def test_a_gate_that_answers_something_else(self):
        srv = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(srv.close)

        def answer():
            sock, _ = srv.accept()
            sock.recv(64)
            sock.sendall(wire.build(0x4004, 1, b"x" * 16))
            sock.close()
        threading.Thread(target=answer, daemon=True).start()
        with self.assertRaises(OSError) as e:
            smoke.server_list("127.0.0.1", srv.getsockname()[1])
        self.assertIn("0x4004", str(e.exception))

if __name__ == "__main__":
    unittest.main()
