"""setup.py --update --yes, the way an OpenLobby panel's deployer runs it: no terminal, stdin
closed, the summary as the last line and the exit status as the verdict. Docker and GitHub are
stand-ins; the server folder is a temporary one."""
import io
import json
import os
import shutil
import tempfile
import unittest
import zipfile
from unittest import mock

import setup
from hostsetup import progress as proglib

OLD_COMMIT = "a" * 40
NEW_COMMIT = "b" * 40


class Result:
    def __init__(self, code=0, out=""):
        self.returncode, self.stdout = code, out


class Docker:
    """docker and docker compose: `running` services, a build that works or not."""

    def __init__(self, running=("gate", "web"), build_ok=True):
        self.running, self.build_ok = list(running), build_ok
        self.calls, self.built_from, self.checked = [], [], []
        self.check_ok = True

    def run(self, cmd, check=True, capture=False, **kw):
        self.calls.append(cmd)
        if cmd[:3] == ["docker", "compose", "ps"]:
            return Result(0, "\n".join(self.running) + "\n")
        if cmd[:4] == ["docker", "compose", "exec", "-T"]:
            self.checked.append(kw.get("input", ""))
            if not self.check_ok:
                return Result(1, "The gate on port 5731 does not answer as it should (refused).\n")
            return Result(0, "The server answers as it should; it gives players the address 192.0.2.10.\n")
        return Result(0, "")

    def run_logged(self, cmd, log, on_line=None, progress=None, cwd=None, env=None):
        self.calls.append(cmd)
        if cmd[:3] == ["docker", "compose", "build"]:
            with open(os.path.join(cwd, "compose.yml")) as f:
                self.built_from.append((cwd, f.read()))
            return (0, ["built"]) if self.build_ok else (1, ["ERROR: failed to solve: process did not complete"])
        return 0, []

    def ups(self):
        return [c for c in self.calls if c[:3] == ["docker", "compose", "up"]]


def release_zip(path, compose, extra=None):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("popndm-x/compose.yml", compose)
        z.writestr("popndm-x/setup.py", "# setup\n")
        z.writestr("popndm-x/popn/gate.py", "# gate " + compose + "\n")
        z.writestr("popndm-x/hostsetup/smoke.py", "def check(web_host=None):  # %s\n    pass\n" % compose)
        for rel, text in (extra or {}).items():
            z.writestr("popndm-x/" + rel, text)


class Update(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.root = os.path.join(self.dir, "Popn Server")
        os.makedirs(os.path.join(self.root, "state", "data"))
        saved = setup.ROOT
        setup.use_root(self.root)
        self.addCleanup(setup.use_root, saved)
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        old = os.path.join(self.dir, "old.zip")
        release_zip(old, "old", {"popn/gone.py": "x"})
        setup.unpack_server(old, self.root, "v1.0.0")
        setup.write_build_info(self.root, OLD_COMMIT)
        self.write(".env", "POPN_ADVERTISE=192.0.2.10\n")
        self.write("state/data/accounts.json", '{"accounts": {}}')
        self.out = io.StringIO()
        self.docker = Docker()
        self.release = ("v1.1.0", NEW_COMMIT)
        self.offline = False
        for target, value in (("docker_state", mock.Mock(return_value="ok")),
                              ("run", self.docker.run),
                              ("github", self.github), ("download", self.download),
                              ("wait_ready", mock.Mock(return_value=True))):
            p = mock.patch.object(setup, target, value)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch.object(setup.proglib, "run_logged", self.docker.run_logged),
                  # from a panel's deployer 127.0.0.1 is the deployer: the update must not ask it
                  mock.patch.object(setup.smokelib, "check", side_effect=AssertionError("asked 127.0.0.1")),
                  mock.patch("sys.stdout", self.out),
                  # stdin is closed: any question would be a bug
                  mock.patch("builtins.input", side_effect=AssertionError("asked a question"))):
            p.start()
            self.addCleanup(p.stop)

    def write(self, rel, text):
        with open(os.path.join(self.root, rel), "w") as f:
            f.write(text)

    def read(self, rel):
        with open(os.path.join(self.root, rel)) as f:
            return f.read()

    def github(self, path):
        if self.offline:
            raise OSError("Network is unreachable")
        tag, commit = self.release
        if path == "/repos/PSChuze/popndm/releases/latest":
            return {"tag_name": tag}
        if path == "/repos/PSChuze/popndm/commits/%s" % tag:
            return {"sha": commit}
        raise AssertionError(path)

    def download(self, url, path):
        self.assertEqual(url, "https://github.com/PSChuze/popndm/archive/refs/tags/v1.1.0.zip")
        release_zip(path, "new")

    def main(self):
        with self.assertRaises(SystemExit) as e:
            setup.main(["--update", "--yes", "--folder", self.root])
        lines = self.out.getvalue().rstrip("\n").splitlines()
        self.assertNotIn("\u2014", self.out.getvalue())
        return e.exception.code, lines[-1]

    def test_already_current(self):
        self.release = ("v1.0.0", OLD_COMMIT)
        self.assertEqual(self.main(), (0, "Already up to date (v1.0.0)."))
        self.assertEqual(self.docker.built_from, [])
        self.assertEqual(self.docker.ups(), [])

    def test_same_commit_under_a_new_tag_is_current(self):
        self.release = ("v1.0.1", OLD_COMMIT)
        self.assertEqual(self.main()[0], 0)
        self.assertEqual(self.read("compose.yml"), "old")

    def test_update_swaps_rebuilds_and_restarts_only_what_ran(self):
        self.docker.running = ["gate"]
        self.assertEqual(self.main(), (0, "Updated to v1.1.0 and restarted gate."))
        # built from the new files before they replaced the old ones
        [(where, compose)] = self.docker.built_from
        self.assertEqual(compose, "new")
        self.assertNotEqual(os.path.normpath(where), os.path.normpath(self.root))
        self.assertEqual(self.docker.ups(), [["docker", "compose", "up", "-d", "--no-build", "--no-deps", "gate"]])
        self.assertEqual(self.read("compose.yml"), "new")
        self.assertFalse(os.path.exists(os.path.join(self.root, "popn", "gone.py")))
        self.assertEqual(self.read(".env"), "POPN_ADVERTISE=192.0.2.10\n")
        self.assertEqual(self.read("state/data/accounts.json"), '{"accounts": {}}')
        self.assertEqual(self.read(".setup-version").strip(), "v1.1.0")
        self.assertEqual(json.loads(self.read("popn/build.json"))["commit"], NEW_COMMIT)
        self.assertEqual(setup.running_commit(), NEW_COMMIT)
        self.assertIn(["docker", "image", "tag", "popndm:local", "popndm:rollback"], self.docker.calls)
        self.assertFalse(os.path.exists(where))      # the staging folder is gone

    def test_the_check_runs_in_the_gate_container_not_on_127_0_0_1(self):
        self.assertEqual(self.main()[0], 0)
        self.assertIn(["docker", "compose", "exec", "-T", "gate", "python", "-"], self.docker.calls)
        [source] = self.docker.checked
        self.assertIn("def check(web_host=None):  # new", source)      # the new version's check
        self.assertIn("check(web_host='web')", source)
        self.assertIn("The server answers as it should", self.out.getvalue())

    def test_a_failed_check_rolls_back(self):
        self.docker.check_ok = False
        code, last = self.main()
        self.assertEqual(code, 1)
        self.assertTrue(last.startswith("v1.1.0 did not start"), last)
        self.assertEqual(self.read("compose.yml"), "old")

    def test_a_stopped_server_stays_stopped(self):
        self.docker.running = []
        self.assertEqual(self.main(), (0, "Updated to v1.1.0; the server was stopped and stays stopped."))
        self.assertEqual(self.docker.ups(), [])
        self.assertEqual(self.read("compose.yml"), "new")

    def test_build_failure_keeps_the_old_version(self):
        self.docker.build_ok = False
        self.assertEqual(self.main(), (1, "Building v1.1.0 failed; the server still runs v1.0.0."))
        self.assertEqual(self.read("compose.yml"), "old")
        self.assertTrue(os.path.exists(os.path.join(self.root, "popn", "gone.py")))
        self.assertEqual(setup.running_commit(), OLD_COMMIT)
        self.assertEqual(self.docker.ups(), [])

    def test_a_version_that_does_not_start_is_rolled_back(self):
        setup.wait_ready.side_effect = [False, True]
        self.assertEqual(self.main(), (1, "v1.1.0 did not start, so the server went back to v1.0.0."))
        self.assertEqual(self.read("compose.yml"), "old")
        self.assertEqual(setup.running_commit(), OLD_COMMIT)
        self.assertEqual(self.read(".setup-version").strip(), "v1.0.0")
        self.assertIn(["docker", "image", "tag", "popndm:rollback", "popndm:local"], self.docker.calls)
        self.assertEqual(len(self.docker.ups()), 2)

    def test_offline(self):
        self.offline = True
        code, last = self.main()
        self.assertEqual(code, 1)
        self.assertEqual(last, "GitHub did not answer, so nothing was updated; check the internet connection.")
        self.assertEqual(self.read("compose.yml"), "old")

    def test_docker_not_running(self):
        setup.docker_state.return_value = "stopped"
        self.assertEqual(self.main(), (1, "Docker is not available, so nothing was updated."))

    def test_the_menu_shares_the_code(self):
        self.docker.running = ["gate", "web"]
        setup.update()
        self.assertEqual(self.out.getvalue().rstrip("\n").splitlines()[-1],
                         "Updated to v1.1.0 and restarted gate, web.")


class Heartbeat(unittest.TestCase):
    def test_a_pipe_gets_a_line_every_two_minutes(self):
        out = io.StringIO()
        now = [0.0]
        p = proglib.Progress(out, clock=lambda: now[0])
        p.begin("Building the server program", "a few minutes")
        for _ in range(10):
            now[0] += 30
            p.tick()
        p.end()
        self.assertEqual(out.getvalue().splitlines(), [
            "Building the server program (a few minutes)...",
            "  Building the server program (a few minutes): 2 minutes so far",
            "  Building the server program (a few minutes): 4 minutes so far",
            "  Done: Building the server program (5 minutes)"])


class Updater(unittest.TestCase):
    def test_block(self):
        u = setup.updater()
        self.assertEqual(u, {"folder": setup.ROOT, "command": ["python", "setup.py", "--update", "--yes"],
                             "timeout_minutes": 60})
        self.assertTrue(os.path.isabs(u["folder"]))
        self.assertTrue(len(u["command"]) <= 16 and all(len(a) < 200 for a in u["command"]))
        self.assertTrue(os.path.exists(os.path.join(setup.ROOT, u["command"][1])))

    def test_a_moved_folder_is_sent_again(self):
        out = io.StringIO()
        choices = {"panel": "http://127.0.0.1:8090", "updater_folder": "/old/place"}
        with mock.patch.object(setup, "read_env", return_value={"OPENLOBBY_PAIRING": "sec",
                                                                "POPN_ADMIN_TOKEN": "tok"}), \
                mock.patch.object(setup, "read_choices", return_value=choices), \
                mock.patch.object(setup, "write_choices") as saved, \
                mock.patch.object(setup.panellib, "find", return_value=None), \
                mock.patch.object(setup.panellib, "update",
                                  return_value=(True, "The panel has the new settings. "
                                                + setup.panellib.REQUESTED)) as upd, \
                mock.patch("sys.stdout", out):
            setup.tell_panel_folder()
            upd.assert_called_once_with("http://127.0.0.1:8090", "popn", "sec", "tok", "http://popn:5738",
                                        updater=setup.updater())
            self.assertEqual(saved.call_args[0][0]["updater_folder"], setup.ROOT)
            self.assertIn("Settings > Modules", out.getvalue())
            upd.reset_mock()
            setup.tell_panel_folder()            # same folder now: nothing to send
            upd.assert_not_called()


if __name__ == "__main__":
    unittest.main()
