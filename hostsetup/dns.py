"""The shared DNS resolver: one per machine, OpenLobby's `dns` service.

A console takes one DNS server, so every game server on a machine shares one resolver,
found by its Docker label. Setup reuses the one that is running, or starts it from
dns/compose.yml when there is none, then adds this game's names under its key:

    docker exec <container> python stub.py dns-names add popn <name>...

The resolver answers those names (and their subdomains) with the machine's address and
forwards everything else upstream. It picks up new names within a few seconds.
"""
import os
import subprocess

ROLE = "org.openlobby.role=dns"
PROJECT = "openlobby-dns"
KEY = "popn"
NAMES = ("ps2pzd1.konamionline.com", "ps2pzdweb.konamionline.com")


def _run(cmd, env=None, cwd=None):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                       errors="replace", env=env, cwd=cwd)
    return r.returncode, (r.stdout or "").strip()


def find(run=_run):
    """The name of the running resolver container, or None."""
    code, out = run(["docker", "ps", "--filter", "label=" + ROLE, "--format", "{{.Names}}"])
    names = [n.strip() for n in out.splitlines() if n.strip()] if code == 0 else []
    return names[0] if names else None


def settings(who, lan, public, desktop):
    """The resolver's environment, for a resolver this setup starts itself.

    At home every name is answered with the LAN address. For friends over the internet
    the resolver also answers internet clients (for the added names only, so it is not
    an open resolver) with the internet address. Docker Desktop hides where a query
    comes from, so there everyone gets the internet address."""
    if who != "internet":
        return {"POL_ADVERTISE": lan, "POL_ADVERTISE_PUBLIC": "", "POL_DNS_PUBLIC": "0"}
    if desktop:
        return {"POL_ADVERTISE": public, "POL_ADVERTISE_PUBLIC": "", "POL_DNS_PUBLIC": "0"}
    return {"POL_ADVERTISE": lan, "POL_ADVERTISE_PUBLIC": public, "POL_DNS_PUBLIC": "1"}


def start(root, values, run=_run, port=None):
    """Start the resolver from dns/compose.yml as its own compose project. -> (ok, output)."""
    env = dict(os.environ, **values)
    if port:
        env["OPENLOBBY_DNS_PORT"] = str(port)
    compose = os.path.join(root, "dns", "compose.yml")
    code, out = run(["docker", "compose", "-p", PROJECT, "-f", compose, "up", "-d"], env=env, cwd=root)
    return code == 0, out


def add_names(container, run=_run, key=KEY, names=NAMES):
    """Add this game's names. -> ("ok" | "old" | "failed", the command's output).
    "old" is a resolver from before dns-names (an older OpenLobby image)."""
    code, out = run(["docker", "exec", container, "python", "stub.py", "dns-names", "add", key] + list(names))
    if code == 0:
        return "ok", out
    if "usage: stub.py" in out and "dns-names" not in out:
        return "old", out
    return "failed", out


def remove_names(container, run=_run, key=KEY):
    code, out = run(["docker", "exec", container, "python", "stub.py", "dns-names", "remove", key])
    return code == 0, out


def ours(container, run=_run):
    """True when the resolver is the one setup starts (its compose project)."""
    code, out = run(["docker", "inspect", "--format", '{{index .Config.Labels "com.docker.compose.project"}}',
                     container])
    return code == 0 and out.strip() == PROJECT
