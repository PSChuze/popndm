"""The shared port 80: OpenLobby's `http` service, one per machine.

A stock install asks port 80 of ps2pzdweb for its rankings and its patch list (installs
with the ranking patch use port 5732 instead). Port 80 is shared by every game server on
a machine, by host name. Setup reuses the web server that is running, or starts it from
http/compose.yml when there is none and port 80 is free, then has it pass the host on:

    docker exec <container> python stub.py http-hosts add popn http://popn-web:5732 ps2pzdweb.konamionline.com
"""
import os
import socket
import subprocess

ROLE = "org.openlobby.role=http"
PROJECT = "openlobby-http"
KEY = "popn"
UPSTREAM = "http://popn-web:5732"
HOSTS = ("ps2pzdweb.konamionline.com",)
BUSY = ("port is already allocated", "address already in use", "ports are not available",
        "only one usage of each socket address")


def _run(cmd, env=None, cwd=None):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                       errors="replace", env=env, cwd=cwd)
    return r.returncode, (r.stdout or "").strip()


def find(run=_run):
    """The name of the running web server container, or None."""
    code, out = run(["docker", "ps", "--filter", "label=" + ROLE, "--format", "{{.Names}}"])
    names = [n.strip() for n in out.splitlines() if n.strip()] if code == 0 else []
    return names[0] if names else None


def port_taken(port=80, host="127.0.0.1"):
    """True when something on this machine already answers on the port."""
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def start(root, run=_run, taken=port_taken):
    """Start the web server from http/compose.yml as its own compose project.
    -> ("ok" | "busy" | "failed", output); "busy" when another program holds port 80."""
    if taken():
        return "busy", "port 80 is in use by another program"
    compose = os.path.join(root, "http", "compose.yml")
    code, out = run(["docker", "compose", "-p", PROJECT, "-f", compose, "up", "-d"], cwd=root)
    if code == 0:
        return "ok", out
    if any(b in out.lower() for b in BUSY):
        return "busy", out
    return "failed", out


def add_hosts(container, run=_run):
    """-> ("ok" | "old" | "failed", output); "old" is an OpenLobby from before http-hosts."""
    code, out = run(["docker", "exec", container, "python", "stub.py", "http-hosts", "add", KEY, UPSTREAM]
                    + list(HOSTS))
    if code == 0:
        return "ok", out
    if "usage: stub.py" in out and "http-hosts" not in out:
        return "old", out
    return "failed", out


def remove_hosts(container, run=_run):
    code, out = run(["docker", "exec", container, "python", "stub.py", "http-hosts", "remove", KEY])
    return code == 0, out
