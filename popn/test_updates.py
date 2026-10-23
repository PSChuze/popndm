"""updates.py: the admin panel's update notice against a stand-in for GitHub's API.

    python -m unittest popn.test_updates -v            (from the repo root)

The running build is older than, equal to or unknown to the newest release; GitHub answers,
refuses or is out of reach. Nothing here goes on the network.
"""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.request

from . import adminport, gate, updates

RUNNING = "a" * 40
RELEASE = "c" * 40


def commit(sha, subject, date, parents=1):
    return {"sha": sha, "parents": [{"sha": "p"}] * parents,
            "commit": {"message": subject + "\n\nbody", "committer": {"date": date}}}


class GitHub:
    """Answers the three calls the check makes; `behind` is the release's commits after RUNNING."""

    def __init__(self, behind=(), down=False, status=200):
        self.behind, self.down, self.status, self.urls = list(behind), down, status, []

    def __call__(self, url):
        self.urls.append(url)
        if self.down:
            raise OSError("Network is unreachable")
        if self.status != 200:
            return self.status, {"message": "nope"}
        path = url[len(updates.API):]
        if path == "/repos/PSChuze/popndm/releases/latest":
            return 200, {"tag_name": "v1.1.0", "published_at": "2024-03-01T10:00:00Z"}
        if path == "/repos/PSChuze/popndm/commits/v1.1.0":
            return 200, commit(RELEASE if self.behind else RUNNING, "release", "2024-03-01T09:00:00+01:00")
        if path == "/repos/PSChuze/popndm/compare/%s...v1.1.0" % RUNNING:
            return 200, {"ahead_by": len(self.behind), "commits": self.behind}
        return 404, {"message": "Not Found"}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def check(github, revision=RUNNING, clock=None):
    c = updates.UpdateCheck("PSChuze/popndm", revision, "2024-02-01T00:00:00Z", fetch=github,
                            clock=clock or Clock())
    return c.get(wait=5)


class UpdateNotice(unittest.TestCase):
    def test_current(self):
        got = check(GitHub())
        self.assertEqual(got["update"], "current")
        self.assertEqual((got["behind"], got["changes"], got["why"]), (0, [], None))
        self.assertEqual(got["latest"], {"commit": RUNNING, "date": "2024-03-01T08:00:00Z"})
        self.assertEqual(got["running"], {"commit": RUNNING, "built_at": "2024-02-01T00:00:00Z"})
        self.assertEqual(got["how"], "Run pop'n Server Setup and choose Update.")

    def test_available_lists_changes_newest_first_without_merges(self):
        behind = [commit("1" * 40, "gate: first", "2024-02-10T00:00:00Z"),
                  commit("2" * 40, "Merge branch x", "2024-02-11T00:00:00Z", parents=2),
                  commit(RELEASE, "web: second", "2024-02-12T00:00:00Z")]
        got = check(GitHub(behind))
        self.assertEqual((got["update"], got["behind"]), ("available", 3))
        self.assertEqual(got["latest"]["commit"], RELEASE)
        self.assertEqual(got["changes"], [
            {"commit": RELEASE, "date": "2024-02-12T00:00:00Z", "subject": "web: second"},
            {"commit": "1" * 40, "date": "2024-02-10T00:00:00Z", "subject": "gate: first"}])

    def test_at_most_fifty_changes(self):
        behind = [commit("%040x" % i, "change %d" % i, "2024-02-10T00:00:00Z") for i in range(80)]
        got = check(GitHub(behind))
        self.assertEqual((got["behind"], len(got["changes"])), (80, 50))
        self.assertEqual(got["changes"][0]["subject"], "change 79")

    def test_offline(self):
        got = check(GitHub(down=True))
        self.assertEqual(got["update"], "unknown")
        self.assertEqual(got["why"], "GitHub did not answer (OSError)")
        self.assertEqual(got["running"]["commit"], RUNNING)

    def test_refused_and_unknown_commit(self):
        self.assertIn("too many requests", check(GitHub(status=403))["why"])
        self.assertEqual(check(GitHub(status=404))["why"], "GitHub has no release of PSChuze/popndm")
        got = check(GitHub(), revision="")
        self.assertEqual(got["update"], "unknown")
        self.assertIn("build.json", got["why"])

    def test_answers_are_kept(self):
        github, clock = GitHub(), Clock()
        c = updates.UpdateCheck("PSChuze/popndm", RUNNING, fetch=github, clock=clock)
        c.get(wait=5)
        c.get(wait=5)
        self.assertEqual(len(github.urls), 3)
        clock.now += updates.TTL + 1
        c.get()
        c.thread and c.thread.join(5)
        self.assertEqual(len(github.urls), 6)

    def test_a_slow_check_never_holds_up_status(self):
        gate_open = threading.Event()

        def slow(url):
            gate_open.wait(5)
            raise OSError("timed out")
        c = updates.UpdateCheck("PSChuze/popndm", RUNNING, fetch=slow)
        got = c.get(wait=0.01)
        self.assertEqual((got["update"], got["why"]), ("unknown", "still asking GitHub, try again in a moment"))
        gate_open.set()
        c.thread and c.thread.join(5)
        self.assertEqual(c.get()["why"], "GitHub did not answer (OSError)")


class BuildFile(unittest.TestCase):
    def test_running(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "build.json")
        self.assertEqual(updates.running(path), ("", ""))
        with open(path, "w") as f:
            json.dump({"commit": RUNNING.upper(), "built_at": "2024-02-01T00:00:00Z"}, f)
        self.assertEqual(updates.running(path), (RUNNING, "2024-02-01T00:00:00Z"))


class Status(unittest.TestCase):
    def test_status_carries_the_update_object(self):
        saved = adminport.TOKEN, adminport.BIND, adminport.PORT
        adminport.TOKEN, adminport.BIND, adminport.PORT = "tok-0123456789", "127.0.0.1", 0
        notice = updates.UpdateCheck("PSChuze/popndm", RUNNING, fetch=GitHub([commit(RELEASE, "x", "")]))
        port = adminport.build(gate.LOBBY, log=lambda m: None, check=notice)
        _host, n = port.start()
        self.addCleanup(port.stop)
        adminport.TOKEN, adminport.BIND, adminport.PORT = saved
        req = urllib.request.Request("http://127.0.0.1:%d/openlobby/status.json" % n,
                                     headers={"X-Devtool-Token": "tok-0123456789"})
        with urllib.request.urlopen(req, timeout=5) as r:
            doc = json.loads(r.read())
        self.assertTrue(doc["up"])
        self.assertEqual(doc["update"]["update"], "available")
        self.assertEqual(doc["update"]["behind"], 1)

    def test_default_repo_and_off_switch(self):
        saved = adminport.UPDATE_REPO
        self.addCleanup(setattr, adminport, "UPDATE_REPO", saved)
        adminport.UPDATE_REPO = "PSChuze/popndm"
        self.assertEqual(adminport.update_check().repo, "PSChuze/popndm")
        adminport.UPDATE_REPO = ""
        self.assertIsNone(adminport.update_check())


if __name__ == "__main__":
    unittest.main()
