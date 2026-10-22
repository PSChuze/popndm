"""Pairing with an OpenLobby admin panel running on this machine.

The panel is found by its Docker label and shows its owner a one-time code. Setup sends
that code with this game's key, title, admin URL and token; the panel checks the admin
port and answers with a pairing secret, which setup keeps to tell the panel about a new
token later.
"""
import json
import subprocess
import urllib.error
import urllib.request

ROLE = "org.openlobby.role=panel"
ANSWERS = {400: "The panel could not reach this server's admin port",
           403: "The code is wrong, expired or already used",
           404: "The panel does not know this server",
           429: "Too many tries; wait a minute"}


def _run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return r.returncode, r.stdout or ""


def find(run=_run):
    """The panel's URL (its org.openlobby.panel.url label), or None."""
    code, out = run(["docker", "ps", "--filter", "label=" + ROLE, "--format",
                     '{{.Label "org.openlobby.panel.url"}}'])
    urls = [u.strip() for u in out.splitlines() if u.strip()] if code == 0 else []
    return urls[0].rstrip("/") if urls else None


def _post(url, body, timeout=15):
    """-> (HTTP status, the JSON answer or {})."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        data = {}
    return status, data if isinstance(data, dict) else {}


def _why(status, data):
    why = data.get("error") or ANSWERS.get(status) or "The panel answered %d" % status
    return why.rstrip(".") + "."


def pair(panel_url, code, key, title, admin_url, token, post=_post):
    """POST /api/pair. -> (the pairing secret or None, a sentence for the host)."""
    try:
        status, data = post(panel_url + "/api/pair", {"code": code.strip(), "key": key, "title": title,
                                                      "url": admin_url, "token": token})
    except OSError as e:
        return None, "The panel did not answer (%s)." % e
    if status == 200 and data.get("pairing"):
        return data["pairing"], "Paired with the panel."
    return None, _why(status, data)


def update(panel_url, key, pairing, token, admin_url="", post=_post):
    """POST /api/pair/update after a token change. -> (ok, a sentence for the host)."""
    body = {"key": key, "pairing": pairing, "token": token}
    if admin_url:
        body["url"] = admin_url
    try:
        status, data = post(panel_url + "/api/pair/update", body)
    except OSError as e:
        return False, "The panel did not answer (%s)." % e
    if status == 200:
        return True, "The panel has the new token."
    return False, _why(status, data)
