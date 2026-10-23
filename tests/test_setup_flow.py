"""setup.py's first run and menu: what the answers turn into, the ports players need,
the server files of a release, the progress display and the menu's items. Nothing
here talks to Docker."""
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest import mock

import setup
from hostsetup import progress as proglib


class Settings(unittest.TestCase):
    def test_home(self):
        c = setup.first_settings("home", "10.0.0.20", "", {}, desktop=False)
        self.assertEqual((c["POPN_ADVERTISE"], c["POPN_ADVERTISE_MAP"]), ("10.0.0.20", ""))
        self.assertGreaterEqual(len(c["POPN_ADMIN_TOKEN"]), 32)
        self.assertEqual((c["POPN_P2P_RELAY"], c["POPN_AUTH_OPEN"]), (1, 1))

    def test_internet_on_linux_gives_lan_players_the_lan_address(self):
        c = setup.first_settings("internet", "10.0.0.20", "203.0.113.5", {}, desktop=False)
        self.assertEqual(c["POPN_ADVERTISE"], "203.0.113.5")
        self.assertEqual(c["POPN_ADVERTISE_MAP"],
                         "10.0.0.0/8=10.0.0.20,172.16.0.0/12=10.0.0.20,192.168.0.0/16=10.0.0.20")

    def test_internet_on_docker_desktop_has_one_address(self):
        c = setup.first_settings("internet", "10.0.0.20", "203.0.113.5", {}, desktop=True)
        self.assertEqual((c["POPN_ADVERTISE"], c["POPN_ADVERTISE_MAP"]), ("203.0.113.5", ""))

    def test_existing_values_stay(self):
        env = {"POPN_ADMIN_TOKEN": "t" * 40, "POPN_P2P_RELAY": "0", "POPN_AUTH_OPEN": "0"}
        c = setup.first_settings("home", "10.0.0.20", "", env, desktop=False)
        for key in env:
            self.assertNotIn(key, c)

    def test_map_reaches_the_gate_as_meant(self):
        from popn import gate
        c = setup.advertise_settings("internet", "10.0.0.20", "203.0.113.5", desktop=False)
        amap = gate.parse_advertise_map(c["POPN_ADVERTISE_MAP"])
        self.assertEqual(gate.advertise_for("192.168.50.31", amap, c["POPN_ADVERTISE"]), "10.0.0.20")
        self.assertEqual(gate.advertise_for("172.18.0.1", amap, c["POPN_ADVERTISE"]), "10.0.0.20")
        self.assertEqual(gate.advertise_for("198.51.100.4", amap, c["POPN_ADVERTISE"]), "203.0.113.5")

    def test_ports(self):
        self.assertEqual(setup.ports_to_open(), [5730, 5731, 5732])
        self.assertEqual(setup.ports_to_open(dns_ours=True), [5730, 5731, 5732, (53, "UDP")])
        self.assertEqual(setup.port_ranges(setup.GAME_PORTS), [(5730, 5732)])

    def test_who_plays(self):
        self.assertEqual(setup.who_plays({"POPN_ADVERTISE": "10.0.0.5"}, {}), "home")
        self.assertEqual(setup.who_plays({"POPN_ADVERTISE": "203.0.113.5"}, {}), "internet")
        self.assertEqual(setup.who_plays({"POPN_ADVERTISE": "10.0.0.5"}, {"who": "internet"}), "internet")

    def test_players_dns(self):
        self.assertEqual(setup.players_dns({}, {"who": "home", "lan": "10.0.0.20"}), "10.0.0.20")
        self.assertEqual(setup.players_dns({}, {"who": "internet", "lan": "10.0.0.20",
                                                "public": "203.0.113.5"}), "203.0.113.5")

    def test_ipv4_only(self):
        self.assertTrue(setup.is_ipv4("203.0.113.5"))
        for bad in ("play.example.net", "2001:db8::1", "", "203.0.113"):
            self.assertFalse(setup.is_ipv4(bad), bad)


def answers(*lines):
    return mock.patch("builtins.input", side_effect=list(lines))


class FirstRun(unittest.TestCase):
    def setUp(self):
        self.out = io.StringIO()
        for target, value in (("lan_address", "10.0.0.20"), ("public_address", "203.0.113.9"),
                              ("docker_desktop", True)):
            p = mock.patch.object(setup, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch("sys.stdout", self.out)
        p.start()
        self.addCleanup(p.stop)

    def test_internet(self):
        # A dry run takes Windows and macOS to mean Docker Desktop.
        with answers("2", ""), mock.patch.object(setup.sys, "platform", "win32"):
            setup.first_run(dry_run=True)
        out = self.out.getvalue()
        self.assertIn("Question 1 of 2: Who will play on this server?", out)
        self.assertIn("Your internet address is 203.0.113.9.", out)
        self.assertIn("  POPN_ADVERTISE=203.0.113.9\n", out)
        self.assertIn("  POPN_ADVERTISE_MAP=\n", out)
        self.assertIn("POPN_ADMIN_TOKEN: made at random", out)
        self.assertIn("POL_ADVERTISE=203.0.113.9", out)
        self.assertNotIn("\u2014", out)

    def test_internet_on_linux(self):
        with answers("2", ""), mock.patch.object(setup.sys, "platform", "linux"):
            setup.first_run(dry_run=True)
        out = self.out.getvalue()
        self.assertIn("192.168.0.0/16=10.0.0.20", out)
        self.assertIn("POL_ADVERTISE=10.0.0.20, POL_ADVERTISE_PUBLIC=203.0.113.9, POL_DNS_PUBLIC=1", out)

    def test_home(self):
        with answers("", ""):
            setup.first_run(dry_run=True)
        out = self.out.getvalue()
        self.assertIn("This computer's address on your network is 10.0.0.20.", out)
        self.assertIn("  POPN_ADVERTISE=10.0.0.20\n", out)

    def test_a_name_is_refused(self):
        with answers("2", "play.example.net", "203.0.113.7"), mock.patch.object(setup.sys, "platform", "win32"):
            setup.first_run(dry_run=True)
        out = self.out.getvalue()
        self.assertIn("The game needs an IP address here", out)
        self.assertIn("  POPN_ADVERTISE=203.0.113.7\n", out)

    def test_finish_ends_with_the_line_for_players(self):
        setup.finish({"POPN_ADVERTISE": "10.0.0.20"}, {"who": "home", "lan": "10.0.0.20", "dns": "ours"})
        lines = self.out.getvalue().rstrip("\n").splitlines()
        self.assertTrue(lines[-1] == "Tell your players: set DNS 1 to 10.0.0.20 in the game's network settings", lines[-1])
        self.assertIn("network settings and DNS point at this server", self.out.getvalue())

    def test_finish_without_dns_says_so(self):
        setup.finish({"POPN_ADVERTISE": "10.0.0.20"}, {"who": "home", "lan": "10.0.0.20", "dns": "old"})
        self.assertIn("The DNS server is not set up yet", self.out.getvalue())


class Menu(unittest.TestCase):
    def test_items(self):
        titles = [t for t, _ in setup.MENU]
        for wanted in ("Start the server", "Stop the server", "Change who plays (and the address)", "Update",
                       "Make a backup", "Restore a backup", "Show what the server is doing (logs)",
                       "Set up DNS again", "Manage from an OpenLobby admin panel", "Make a new admin token",
                       "Remove the server"):
            self.assertIn(wanted, titles)
        self.assertTrue(all(callable(fn) for _, fn in setup.MENU))

    def test_a_number_runs_its_item(self):
        called = []
        menu = [(t, (lambda t=t: called.append(t))) for t, _ in setup.MENU]
        with mock.patch.object(setup, "MENU", menu), mock.patch.object(setup, "read_env", return_value={}), \
                mock.patch.object(setup, "read_choices", return_value={}), \
                mock.patch.object(setup, "server_running", return_value=False), \
                mock.patch("sys.stdout", io.StringIO()), answers("2", "x", "99", "7", "0"):
            setup.menu()
        self.assertEqual(called, ["Stop the server", "Show what the server is doing (logs)"])

    def test_new_token_tells_a_paired_panel(self):
        writes = []
        with mock.patch.object(setup, "write_env", side_effect=writes.append), \
                mock.patch.object(setup, "server_running", return_value=False), \
                mock.patch.object(setup, "read_env", return_value={"OPENLOBBY_PAIRING": "sec"}), \
                mock.patch.object(setup, "read_choices", return_value={}), \
                mock.patch.object(setup.panellib, "find", return_value="http://127.0.0.1:8090"), \
                mock.patch.object(setup.panellib, "update", return_value=(True, "ok")) as upd, \
                mock.patch.object(setup, "write_choices") as saved, \
                mock.patch("sys.stdout", io.StringIO()), answers("y"):
            setup.change_token()
        token = writes[0]["POPN_ADMIN_TOKEN"]
        upd.assert_called_once_with("http://127.0.0.1:8090", "popn", "sec", token, "http://popn:5738",
                                    updater=setup.updater())
        self.assertEqual(saved.call_args[0][0]["updater_folder"], setup.ROOT)


class TooOld(unittest.TestCase):
    def test_dns_and_http_say_update_it(self):
        out = io.StringIO()
        with mock.patch.object(setup.dnslib, "find", return_value="openlobby-dns-dns-1"),                 mock.patch.object(setup.dnslib, "add_names", return_value=("old", "usage: stub.py {dns|http|tcp}")),                 mock.patch.object(setup.dnslib, "ours", return_value=True),                 mock.patch.object(setup, "write_choices"), mock.patch("sys.stdout", out):
            self.assertFalse(setup.setup_dns({}, {}))
        with mock.patch.object(setup.httplib, "find", return_value="openlobby-http-http-1"),                 mock.patch.object(setup.httplib, "add_hosts", return_value=("old", "usage: stub.py")),                 mock.patch("sys.stdout", out):
            self.assertEqual(setup.share_port_80(), "old")
        self.assertEqual(out.getvalue().count("too old for this, update it"), 2)


class Port80(unittest.TestCase):
    def test_starts_the_web_server_when_none_runs(self):
        with mock.patch.object(setup.httplib, "find", side_effect=[None, "openlobby-http-http-1"]),                 mock.patch.object(setup.httplib, "start", return_value=("ok", "")) as start,                 mock.patch.object(setup.httplib, "add_hosts", return_value=("ok", "")) as add,                 mock.patch.object(setup, "ensure_network"), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(setup.share_port_80(), "ok")
        start.assert_called_once_with(setup.ROOT)
        add.assert_called_once_with("openlobby-http-http-1")

    def test_reuses_a_running_one(self):
        with mock.patch.object(setup.httplib, "find", return_value="openlobby-http-1"),                 mock.patch.object(setup.httplib, "start") as start,                 mock.patch.object(setup.httplib, "add_hosts", return_value=("ok", "")),                 mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(setup.share_port_80(), "ok")
        start.assert_not_called()

    def test_port_80_taken_is_skipped(self):
        out = io.StringIO()
        with mock.patch.object(setup.httplib, "find", return_value=None),                 mock.patch.object(setup.httplib, "start", return_value=("busy", "")),                 mock.patch.object(setup.httplib, "add_hosts") as add,                 mock.patch.object(setup, "ensure_network"), mock.patch("sys.stdout", out):
            self.assertEqual(setup.share_port_80(), "busy")
        add.assert_not_called()
        self.assertIn("Another program on this computer uses port 80", out.getvalue())


class Backups(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.saved = setup.ROOT
        setup.use_root(self.dir)
        self.addCleanup(setup.use_root, self.saved)
        os.makedirs(os.path.join(self.dir, "state", "data"))
        with open(os.path.join(self.dir, "state", "data", "accounts.json"), "w") as f:
            f.write('{"accounts": {}}')

    def test_backup_and_restore(self):
        with mock.patch("sys.stdout", io.StringIO()):
            setup.backup()
        zips = os.listdir(os.path.join(self.dir, "backups"))
        self.assertEqual(len(zips), 1)
        with open(os.path.join(self.dir, "state", "data", "accounts.json"), "w") as f:
            f.write("changed")
        with mock.patch.object(setup, "server_running", return_value=False), \
                mock.patch.object(setup, "compose") as comp, \
                mock.patch("sys.stdout", io.StringIO()), answers("1", "y"):
            setup.restore()
        comp.assert_called_once_with("stop", check=False)
        with open(os.path.join(self.dir, "state", "data", "accounts.json")) as f:
            self.assertEqual(f.read(), '{"accounts": {}}')


class ServerFiles(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def release(self, name, files):
        path = os.path.join(self.dir, name)
        with zipfile.ZipFile(path, "w") as z:
            for rel, text in files.items():
                z.writestr("popndm-1.0/" + rel, text)
        return path

    def test_unpack_and_update(self):
        root = os.path.join(self.dir, "server")
        os.makedirs(os.path.join(root, "state"))
        with open(os.path.join(root, ".env"), "w") as f:
            f.write("mine")
        first = self.release("a.zip", {"compose.yml": "1", "old.py": "x", ".env": "theirs", "state/x": "no",
                                       "../escape": "no"})
        setup.unpack_server(first, root, "v1.0.0")
        self.assertEqual(sorted(setup.read_manifest(root)), ["compose.yml", "old.py"])
        with open(os.path.join(root, ".env")) as f:
            self.assertEqual(f.read(), "mine")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "escape")))
        second = self.release("b.zip", {"compose.yml": "2", "new.py": "y"})
        setup.unpack_server(second, root, "v1.1.0")
        self.assertFalse(os.path.exists(os.path.join(root, "old.py")))
        with open(os.path.join(root, "compose.yml")) as f:
            self.assertEqual(f.read(), "2")
        with open(os.path.join(root, ".setup-version")) as f:
            self.assertEqual(f.read().strip(), "v1.1.0")

    def test_root(self):
        self.assertEqual(setup.find_root(self.dir), os.path.abspath(self.dir))
        self.assertEqual(setup.find_root(), os.path.dirname(os.path.abspath(setup.__file__)))


class Progress(unittest.TestCase):
    def test_lines(self):
        out = io.StringIO()
        now = [0.0]
        p = proglib.Progress(out, clock=lambda: now[0])
        p.begin("Starting the server", "under a minute")
        now[0] += 90
        self.assertEqual(p.line(), "  Starting the server (under a minute): 1 minute so far")
        p.end()
        self.assertEqual(out.getvalue(), "Starting the server (under a minute)...\n"
                                         "  Done: Starting the server (1 minute)\n")

    def test_run_logged(self):
        log = io.StringIO()
        seen = []
        code, tail = proglib.run_logged([sys.executable, "-c", "print('a'); print('b'); raise SystemExit(3)"],
                                        log, on_line=seen.append)
        self.assertEqual((code, tail, seen), (3, ["a", "b"], ["a", "b"]))
        self.assertEqual(log.getvalue().replace("\r", ""), "a\nb\n")

    def test_explain(self):
        self.assertIn("ports", proglib.explain(["Bind for 0.0.0.0:5731 failed: port is already allocated"]))
        self.assertIn("openlobby", proglib.explain(["network openlobby declared as external, but could not "
                                                    "be found"]))
        self.assertIn("disk is full", proglib.explain(["OSError: [Errno 28] No space left on device"]))
        self.assertEqual(proglib.explain(["something else"]), "")


class Docker(unittest.TestCase):
    def test_missing_docker_explains_and_returns(self):
        out = io.StringIO()
        with mock.patch.object(setup, "docker_state", return_value="missing"), \
                mock.patch("sys.platform", "win32"), mock.patch("sys.stdout", out):
            self.assertFalse(setup.wait_for_docker(wait=False))
        self.assertIn(setup.DOCKER_DOWNLOAD, out.getvalue())

    def test_waits_until_docker_runs(self):
        out = io.StringIO()
        with mock.patch.object(setup, "docker_state", side_effect=["stopped", "ok"]), \
                mock.patch.object(setup, "start_docker_desktop", return_value=False), \
                mock.patch("sys.platform", "darwin"), mock.patch("sys.stdout", out), answers(""):
            self.assertTrue(setup.wait_for_docker())
        self.assertIn("Docker Desktop is installed but not running", out.getvalue())


if __name__ == "__main__":
    unittest.main()
