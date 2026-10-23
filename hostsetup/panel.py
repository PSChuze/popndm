"""Pairing with an OpenLobby admin panel running on this machine.

The panel is found by its Docker label (with several, setup asks which one, or stops when it
cannot ask) and shows its owner a one-time code. Setup sends
that code with this game's key, title, admin URL and token; the panel checks the admin
port and answers with a pairing secret, which setup keeps to tell the panel about a new
token later.

Both calls also offer the panel an updater: the server folder and the command that updates
it (setup.py --update --yes), which the panel's deployer runs when its owner presses Update.
The panel answers "requested" until its owner approves that on Settings > Modules.
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
REQUESTED = "The panel's owner must approve updates from the panel on Settings > Modules."


def _run(cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    return r.returncode, r.stdout or ""


def panels(run=_run):
    """Every running panel as (container name, URL from its org.openlobby.panel.url label)."""
    code, out = run(["docker", "ps", "--filter", "label=" + ROLE, "--format",
                     '{{.Names}}\t{{.Label "org.openlobby.panel.url"}}'])
    found = []
    for line in out.splitlines() if code == 0 else []:
        name, _, url = line.strip().partition("\t")
        if url.strip():
            found.append((name.strip(), url.strip().rstrip("/")))
    return found


def find(run=_run):
    """The panel's URL when exactly one panel runs here, else None (setup never guesses)."""
    found = panels(run)
    return found[0][1] if len(found) == 1 else None


def several(found):
    """The sentence for a run that cannot ask which of several panels to pair with."""
    shown = ", ".join("%s at %s" % (name, url) for name, url in found)
    return ("Not paired: more than one OpenLobby admin panel is running here (%s); "
            "run pop'n Server Setup and choose the panel." % shown)


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


def _approve(message, data):
    """The message, with the approval note when the panel holds the updater for its owner."""
    return message + (" " + REQUESTED if data.get("updater") == "requested" else "")


def pair(panel_url, code, key, title, admin_url, token, updater=None, post=_post):
    """POST /api/pair. -> (the pairing secret or None, a sentence for the host)."""
    body = {"code": code.strip(), "key": key, "title": title, "url": admin_url, "token": token}
    if updater:
        body["updater"] = updater
    try:
        status, data = post(panel_url + "/api/pair", body)
    except OSError as e:
        return None, "The panel did not answer (%s)." % e
    if status == 200 and data.get("pairing"):
        return data["pairing"], _approve("Paired with the panel.", data)
    return None, _why(status, data)


def update(panel_url, key, pairing, token, admin_url="", updater=None, post=_post):
    """POST /api/pair/update after a token or folder change. -> (ok, a sentence for the host)."""
    body = {"key": key, "pairing": pairing, "token": token}
    if admin_url:
        body["url"] = admin_url
    if updater:
        body["updater"] = updater
    try:
        status, data = post(panel_url + "/api/pair/update", body)
    except OSError as e:
        return False, "The panel did not answer (%s)." % e
    if status == 200:
        return True, _approve("The panel has the new settings.", data)
    return False, _why(status, data)
