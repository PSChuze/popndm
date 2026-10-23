"""Is a newer server out? The admin panel's update notice (adminport.py, status.json).

The running build's commit comes from popn/build.json, which the setup program writes when it
unpacks or updates the server's files, so it is copied into the image with the package. It is
compared with the newest release of a public GitHub repository (POPN_UPDATE_REPO, "owner/name",
default PSChuze/popndm; empty turns the check off) through GitHub's REST API, without a token:
GET /repos/<repo>/releases/latest for the newest release, /commits/<tag> for its commit and
/compare/<running>...<tag> for how far behind the build is and what changed. The answer is kept
for half an hour (ten minutes when the check failed: an unauthenticated address gets 60 calls an
hour) and is fetched on a thread of its own, so status.json waits at most WAIT seconds. Every
failure is an "unknown" answer with a reason, never an error.
"""
import datetime
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
HOW = "Run pop'n Server Setup and choose Update."
BUILD_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build.json")
TTL = 1800.0             # s an answer is kept
TTL_FAILED = 600.0       # s a failed check is kept before trying again
WAIT = 0.8               # s status.json waits for a check in flight
TIMEOUT = 8.0            # s per GitHub request
MAX_CHANGES = 50
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


class Failed(Exception):
    """A check that ended without an answer; the message is the panel's "why"."""


def running(path=BUILD_FILE):
    """(commit, built_at) of this build from build.json, or ("", "") when it is missing."""
    try:
        with open(path, encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        return "", ""
    if not isinstance(info, dict):
        return "", ""
    return str(info.get("commit") or "").strip().lower(), str(info.get("built_at") or "").strip()


def fetch(url):
    """GET a GitHub API url -> (HTTP status, parsed JSON or None)."""
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "X-GitHub-Api-Version": "2022-11-28",
                                               "User-Agent": "popndm-update-check"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except ValueError:
            body = None
        return e.code, body


def iso(stamp):
    """A GitHub date ("2024-01-02T12:00:00Z" or with an offset) as ISO 8601 UTC, or ""."""
    if not isinstance(stamp, str) or not stamp:
        return ""
    try:
        t = datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    return t.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _date(c):
    commit = c.get("commit") or {}
    return iso((commit.get("committer") or {}).get("date") or (commit.get("author") or {}).get("date"))


def _subject(c):
    return str((c.get("commit") or {}).get("message") or "").split("\n", 1)[0].strip()[:200]


def unknown(why):
    return {"latest": None, "behind": None, "changes": [], "update": "unknown", "why": why, "how": HOW}


class UpdateCheck:
    """The update object for one repository and running commit, cached."""

    def __init__(self, repo, revision, built_at="", fetch=None, clock=time.monotonic):
        self.repo = (repo or "").strip()
        self.revision = (revision or "").strip().lower()
        self.built_at = (built_at or "").strip()
        self.fetch = fetch or globals()["fetch"]
        self.clock = clock
        self.lock = threading.Lock()
        self.answer = None        # the last check's result (without "running")
        self.until = 0.0          # clock time it goes stale
        self.thread = None

    def get(self, wait=WAIT):
        """The update object now: the cached answer, refreshed in the background when stale."""
        with self.lock:
            stale = self.answer is None or self.clock() >= self.until
            if stale and self.thread is None:
                self.thread = threading.Thread(target=self._refresh, name="popn-update-check", daemon=True)
                self.thread.start()
            thread, answer = self.thread, self.answer
        if answer is None and thread is not None:
            thread.join(wait)
            with self.lock:
                answer = self.answer
        if answer is None:
            answer = unknown("still asking GitHub, try again in a moment")
        return dict(answer, running={"commit": self.revision, "built_at": self.built_at})

    def _refresh(self):
        try:
            answer, ttl = self.check(), TTL
        except Failed as e:
            answer, ttl = unknown(str(e)), TTL_FAILED
        except Exception as e:   # never let the check break status.json
            answer, ttl = unknown("the check failed (%s)" % type(e).__name__), TTL_FAILED
        with self.lock:
            self.answer, self.until, self.thread = answer, self.clock() + ttl, None

    def _get(self, path):
        try:
            status, body = self.fetch(API + path)
        except (OSError, ValueError) as e:   # offline, timeouts, refused connections, bad JSON
            raise Failed("GitHub did not answer (%s)" % type(e).__name__)
        if status in (403, 429):
            raise Failed("GitHub refused the check for now (too many requests from this address)")
        return status, body

    def check(self):
        """Ask GitHub -> the answer (latest, behind, changes, update, why, how). Raises Failed."""
        if not REPO_RE.match(self.repo):
            raise Failed("POPN_UPDATE_REPO is not in the form owner/name")
        if not SHA_RE.match(self.revision):
            raise Failed("this build does not know its commit (popn/build.json is missing)")
        status, rel = self._get("/repos/%s/releases/latest" % self.repo)
        if status == 404:
            raise Failed("GitHub has no release of %s" % self.repo)
        if status != 200 or not isinstance(rel, dict) or not rel.get("tag_name"):
            raise Failed("GitHub gave an unexpected answer (%s)" % status)
        tag = urllib.parse.quote(str(rel["tag_name"]), safe="")
        status, head = self._get("/repos/%s/commits/%s" % (self.repo, tag))
        if status != 200 or not isinstance(head, dict) or not head.get("sha"):
            raise Failed("GitHub gave an unexpected answer (%s)" % status)
        latest = {"commit": str(head["sha"]), "date": _date(head)}
        status, cmp = self._get("/repos/%s/compare/%s...%s" % (self.repo, self.revision, tag))
        if status == 404:
            raise Failed("this build is not from the public repository")
        if status != 200 or not isinstance(cmp, dict) or not isinstance(cmp.get("ahead_by"), int):
            raise Failed("GitHub gave an unexpected answer (%s)" % status)
        # base = the running commit, head = the release: its commits the build lacks are "ahead"
        behind = cmp["ahead_by"]
        changes = [{"commit": str(c.get("sha") or ""), "date": _date(c), "subject": _subject(c)}
                   for c in reversed(cmp.get("commits") or [])
                   if isinstance(c, dict) and len(c.get("parents") or []) < 2]
        return {"latest": latest, "behind": behind, "changes": changes[:MAX_CHANGES],
                "update": "available" if behind > 0 else "current", "why": None, "how": HOW}
