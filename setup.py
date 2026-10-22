"""pop'n Server Setup: python setup.py (or the "pop'n Server Setup" program)

The first run asks who plays and checks the address players reach, writes .env, starts
the server in Docker, adds the game's names to the shared DNS resolver and checks the
server. Later runs show a menu: start and stop, settings, update, backups, logs, the
OpenLobby panel. Needs Docker and nothing else (Python 3.8+ when run as a script,
standard library only).
"""
import argparse
import datetime
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile

from hostsetup import dns as dnslib
from hostsetup import http as httplib
from hostsetup import panel as panellib
from hostsetup import progress as proglib
from hostsetup import smoke as smokelib
from hostsetup import upnp

PROGRAM = "pop'n Server Setup"
REPO = os.environ.get("POPN_SETUP_REPO", "PSChuze/popndm")
RELEASES = "https://github.com/%s/releases" % REPO
DOCKER_DOWNLOAD = "https://www.docker.com/products/docker-desktop/"
DOCKER_LINUX = "https://docs.docker.com/engine/install/"
PORT_HELP = "https://portforward.com/router.htm"
DEFAULT_FOLDER = "Popn Server"
FROZEN = getattr(sys, "frozen", False)

KEY = "popn"
TITLE = "pop'n Puzzle-dama Online"
ADMIN_URL = "http://popn:5738"
NETWORK = "openlobby"
# The game dials these: gate, battle relay, rankings (fixed in compose.yml).
GAME_PORTS = (5730, 5731, 5732)
PRIVATE_NETS = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
# Files of ours in the server folder that an update must never replace.
KEEP = (".env", "compose.override.yml", "state", "backups", "logs")


def use_root(path):
    """Point every path at the server folder `path`."""
    global ROOT, ENV, ENV_EXAMPLE, BACKUPS, CHOICES, LOG, VERSION_FILE
    ROOT = os.path.abspath(path)
    ENV = os.path.join(ROOT, ".env")
    ENV_EXAMPLE = os.path.join(ROOT, ".env.example")
    BACKUPS = os.path.join(ROOT, "backups")
    CHOICES = os.path.join(ROOT, "state", "setup.json")
    LOG = os.path.join(ROOT, "logs", "setup.log")
    VERSION_FILE = os.path.join(ROOT, ".setup-version")


use_root(os.path.dirname(os.path.abspath(__file__)))


# --- terminal ----------------------------------------------------------------------------

def say(text=""):
    print(text, flush=True)


def ask(question, default=""):
    shown = " [%s]" % default if default else ""
    try:
        answer = input("%s%s: " % (question, shown)).strip()
    except EOFError:
        answer = ""
    return answer or default


def ask_yes(question, default=True):
    answer = ask(question + (" (Y/n)" if default else " (y/N)")).lower()
    return default if not answer else answer.startswith("y")


def ask_choice(question, options, default=1):
    """Number options from 1 and return the 1-based pick."""
    if question:
        say(question)
    for i, text in enumerate(options, 1):
        say("  %d. %s" % (i, text))
    while True:
        answer = ask("Type a number", str(default))
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return int(answer)
        say("  Type one of the numbers above.")


def heading(text):
    say()
    say(text)
    say("-" * len(text))


def log_file():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    f = open(LOG, "a", encoding="utf-8")
    f.write("\n=== %s ===\n" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    return f


# --- the server's files -------------------------------------------------------------------

def find_root(args_folder=None):
    """The server folder: the one given, else the folder this runs from when it
    holds the server, else "Popn Server" in the home folder (made when missing)."""
    if args_folder:
        return os.path.abspath(args_folder)
    if not FROZEN:
        return os.path.dirname(os.path.abspath(__file__))
    for d in (os.path.dirname(sys.executable), os.getcwd()):
        if os.path.exists(os.path.join(d, "compose.yml")):
            return d
    return os.path.join(os.path.expanduser("~"), DEFAULT_FOLDER)


def bundled(name):
    """A file packed into the setup program, or None."""
    base = getattr(sys, "_MEIPASS", None)
    path = os.path.join(base, name) if base else None
    return path if path and os.path.exists(path) else None


def unpack_server(archive, root, version=""):
    """Unpack a source zip of this repository into root, replacing the files
    the last one put there and leaving the host's own (.env, state, ...) alone.
    Returns the paths it wrote, relative to root."""
    with zipfile.ZipFile(archive) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        # GitHub's and git archive's zips put everything in one top folder.
        tops = {n.split("/", 1)[0] for n in z.namelist()}
        strip = len(tops) == 1 and all("/" in n for n in names)
        old = set(read_manifest(root))
        written = []
        for name in names:
            rel = name.split("/", 1)[1] if strip else name
            if not rel or rel.split("/", 1)[0] in KEEP:
                continue
            target = os.path.normpath(os.path.join(root, rel))
            if not target.startswith(os.path.normpath(root) + os.sep):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(name) as src, open(target + ".tmp", "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(target + ".tmp", target)
            written.append(rel)
    for rel in old - set(written):
        try:
            os.remove(os.path.join(root, rel))
        except OSError:
            pass
    with open(os.path.join(root, ".setup-files"), "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(sorted(written)) + "\n")
    if version:
        with open(os.path.join(root, ".setup-version"), "w", encoding="utf-8") as f:
            f.write(version + "\n")
    return written


def read_manifest(root):
    try:
        with open(os.path.join(root, ".setup-files"), encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


def installed_version():
    try:
        with open(VERSION_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def ensure_server_files():
    """Put the server's files in ROOT when they are not there yet."""
    if os.path.exists(os.path.join(ROOT, "compose.yml")):
        return True
    archive = bundled("server.zip")
    if not archive:
        say("The server's files are not in %s, and this setup program does not carry them." % ROOT)
        say("Download %s again from %s" % (PROGRAM, RELEASES))
        return False
    os.makedirs(ROOT, exist_ok=True)
    version = ""
    if bundled("server-version.txt"):
        with open(bundled("server-version.txt"), encoding="utf-8") as f:
            version = f.read().strip()
    unpack_server(archive, ROOT, version)
    say("The server's files are in %s" % ROOT)
    return True


def latest_release():
    """(tag, source zip URL) of the newest release, or None."""
    url = "https://api.github.com/repos/%s/releases/latest" % REPO
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"}),
                                    timeout=10) as r:
            info = json.load(r)
    except (OSError, ValueError):
        return None
    tag = info.get("tag_name")
    return (tag, "https://github.com/%s/archive/refs/tags/%s.zip" % (REPO, tag)) if tag else None


# --- docker ------------------------------------------------------------------------------

def run(cmd, check=True, capture=False, **kw):
    """Run a command in the server folder; its output goes to the terminal unless captured."""
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    result = subprocess.run(cmd, cwd=ROOT if os.path.isdir(ROOT) else None, **kw)
    if check and result.returncode != 0:
        raise SystemExit("Failed (exit %d): %s" % (result.returncode, " ".join(cmd)))
    return result


def compose(*args, **kw):
    return run(["docker", "compose"] + list(args), **kw)


def add_docker_to_path():
    """Docker installed while this window was open is not on its PATH yet."""
    places = [os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker", "resources",
                           "bin"),
              "/usr/local/bin", "/Applications/Docker.app/Contents/Resources/bin",
              os.path.join(os.path.expanduser("~"), ".docker", "bin")]
    path = os.environ.get("PATH", "").split(os.pathsep)
    for p in places:
        if os.path.isdir(p) and p not in path:
            path.append(p)
    os.environ["PATH"] = os.pathsep.join(path)


def docker_state():
    """"ok", "missing", "stopped", "denied" (Linux user not allowed) or "old"."""
    add_docker_to_path()
    if not shutil.which("docker"):
        return "missing"
    try:
        info = run(["docker", "info"], check=False, capture=True, timeout=60)
    except subprocess.TimeoutExpired:
        return "stopped"
    if info.returncode != 0:
        return "denied" if "permission denied" in (info.stdout or "").lower() else "stopped"
    if run(["docker", "compose", "version"], check=False, capture=True).returncode != 0:
        return "old"
    return "ok"


def start_docker_desktop():
    """Ask Docker Desktop to start; True when there was one to start."""
    if os.name == "nt":
        exe = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Docker", "Docker",
                           "Docker Desktop.exe")
        if os.path.exists(exe):
            subprocess.Popen([exe], close_fds=True)
            return True
    elif sys.platform == "darwin" and os.path.isdir("/Applications/Docker.app"):
        return subprocess.run(["open", "-a", "Docker"]).returncode == 0
    return False


def wait_for_docker(wait=True):
    """Return once Docker works. Explains what is missing and waits for the
    host to fix it (or returns False at once with wait=False)."""
    started = False
    while True:
        state = docker_state()
        if state == "ok":
            return True
        say()
        if state == "missing":
            if sys.platform.startswith("linux"):
                say("Docker is not installed. Install Docker Engine: %s" % DOCKER_LINUX)
            else:
                say("Docker Desktop is not installed. This server runs inside it.")
                say("Download it here, install it and start it: %s" % DOCKER_DOWNLOAD)
                say("If it asks you to sign in, you can skip that.")
        elif state == "stopped" and not started and wait and start_docker_desktop():
            started = True
            say("Starting Docker Desktop. This can take a minute or two.")
            for _ in range(60):
                time.sleep(3)
                if docker_state() == "ok":
                    say("Docker Desktop is running.")
                    return True
            say("Docker Desktop has not started yet. If it shows a message (terms to accept,")
            say("an update to install), answer it.")
        elif state == "stopped":
            if sys.platform.startswith("linux"):
                say("Docker is installed but not running. Start it with: sudo systemctl start docker")
            else:
                say("Docker Desktop is installed but not running. Open it and wait until it says")
                say("it is running.")
        elif state == "denied":
            say("Your user may not use Docker yet. Run this, then log out and back in:")
            say("  sudo usermod -aG docker $USER")
        else:
            say("This Docker is too old (it has no 'docker compose'). Update it: %s" % DOCKER_DOWNLOAD)
        if not wait:
            return False
        ask("Press Enter to check again (or close this window to stop)")


def docker_desktop():
    """True on Docker Desktop, which hides the players' addresses behind one."""
    out = run(["docker", "info", "--format", "{{.OperatingSystem}}"], check=False, capture=True).stdout
    return "docker desktop" in (out or "").lower()


def ensure_network():
    """The shared network an OpenLobby panel reaches the admin port on."""
    if run(["docker", "network", "inspect", NETWORK], check=False, capture=True).returncode != 0:
        run(["docker", "network", "create", NETWORK], capture=True)


def server_running():
    out = compose("ps", "--status", "running", "--services", check=False, capture=True).stdout or ""
    return "gate" in out.split()


def wait_ready(timeout=120, quiet=False):
    """Wait for the gate's and the rankings' start lines; False when they do not come."""
    if not quiet:
        say("Waiting for the server to start...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        logs = compose("logs", "--no-log-prefix", "--tail", "200", "gate", "web", check=False,
                       capture=True).stdout or ""
        if "popn gate on" in logs and "popn web (ps2pzdweb) on" in logs:
            return True
        if "Traceback" in logs:
            break
        time.sleep(2)
    say("The server did not start. Its last lines:")
    say(compose("logs", "--tail", "40", "gate", "web", check=False, capture=True).stdout)
    return False


def smoke(quiet=False):
    if not quiet:
        say("Checking the server...")
    ok, message = smokelib.check()
    say(message)
    return ok


# --- settings ----------------------------------------------------------------------------

def read_env(path=None):
    path = path or ENV
    values = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = re.match(r"\s*([A-Z_][A-Z0-9_]*)=(.*)$", line.rstrip("\n"))
                if m:
                    values[m.group(1)] = m.group(2).strip()
    return values


def write_env(values, path=None, template=None):
    """Write values into the template's lines (a commented-out `#KEY=` line is switched on,
    a value of None comments a line out); keys the template does not have go at the end.
    Comments stay as they are."""
    path, template = path or ENV, template or ENV_EXAMPLE
    source = path if os.path.exists(path) else template
    with open(source, encoding="utf-8") as f:
        lines = f.read().splitlines()
    left = dict(values)
    out = []
    for line in lines:
        m = re.match(r"(#?)([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(2) in left:
            value = left.pop(m.group(2))
            out.append(("#" + line.lstrip("#")) if value is None else "%s=%s" % (m.group(2), value))
            continue
        out.append(line)
    for key, value in left.items():
        if value is not None:
            out.append("%s=%s" % (key, value))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, path)


def read_choices():
    try:
        with open(CHOICES, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_choices(choices):
    os.makedirs(os.path.dirname(CHOICES), exist_ok=True)
    with open(CHOICES, "w", encoding="utf-8", newline="\n") as f:
        json.dump(choices, f, indent=1)
        f.write("\n")


def lan_address():
    """This machine's address on the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
    except OSError:
        return ""


def public_address():
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=4) as r:
            ip = r.read().decode().strip()
        return ip if is_ipv4(ip) else ""
    except (OSError, ValueError):
        return ""


def is_ipv4(text):
    try:
        return isinstance(ipaddress.ip_address(text.strip()), ipaddress.IPv4Address)
    except ValueError:
        return False


def who_plays(env, choices):
    """"home" or "internet": as chosen, or guessed from the address."""
    if choices.get("who") in ("home", "internet"):
        return choices["who"]
    return "home" if upnp.is_private(env.get("POPN_ADVERTISE", "")) else "internet"


def advertise_settings(who, lan, public, desktop):
    """POPN_ADVERTISE and POPN_ADVERTISE_MAP: the address the gate hands each player.

    At home that is this machine's LAN address. Over the internet it is the internet
    address, except for players on the LAN, who get the LAN address. Docker Desktop
    shows every player as one private address, so there everyone gets the internet
    address and LAN players rely on the router passing it back in."""
    if who != "internet":
        return {"POPN_ADVERTISE": lan, "POPN_ADVERTISE_MAP": ""}
    if desktop or not lan:
        return {"POPN_ADVERTISE": public, "POPN_ADVERTISE_MAP": ""}
    return {"POPN_ADVERTISE": public, "POPN_ADVERTISE_MAP": ",".join("%s=%s" % (net, lan) for net in PRIVATE_NETS)}


def new_token():
    return secrets.token_urlsafe(32)


def first_settings(who, lan, public, env, desktop):
    """The .env values for the answers. Everything not asked keeps its value in env
    (the template on a first run) or gets the usual default."""
    changes = advertise_settings(who, lan, public, desktop)
    if not env.get("POPN_ADMIN_TOKEN"):
        changes["POPN_ADMIN_TOKEN"] = new_token()
    if env.get("POPN_P2P_RELAY") in (None, ""):
        changes["POPN_P2P_RELAY"] = 1
    if env.get("POPN_AUTH_OPEN") in (None, ""):
        changes["POPN_AUTH_OPEN"] = 1
    return changes


def ports_to_open(dns_ours=False):
    """The ports players must reach: TCP numbers, and DNS when this setup runs the resolver."""
    ports = list(GAME_PORTS)
    if dns_ours:
        ports.append((53, "UDP"))
    return ports


def port_ranges(ports):
    """[(first, last)] runs of consecutive ports, for a short table."""
    out = []
    for p in sorted(set(ports)):
        if out and p == out[-1][1] + 1:
            out[-1] = (out[-1][0], p)
        else:
            out.append((p, p))
    return out


def state_dir(env):
    return os.path.join(ROOT, env.get("POPN_STATE") or "state")


def players_dns(env, choices):
    """The DNS server address players type: this machine's, as seen from where they are."""
    if who_plays(env, choices) == "internet":
        return choices.get("public") or env.get("POPN_ADVERTISE", "")
    return choices.get("lan") or env.get("POPN_ADVERTISE", "")


# --- the questions -----------------------------------------------------------------------

def ask_who(current="home"):
    heading("Question 1 of 2: Who will play on this server?")
    pick = ask_choice("", ["Just people on my home network", "Friends over the internet"],
                      2 if current == "internet" else 1)
    return "internet" if pick == 2 else "home"


def ask_ip(question, default):
    while True:
        answer = ask(question, default)
        if is_ipv4(answer):
            return answer.strip()
        say("  The game needs an IP address here (four numbers with dots, like 203.0.113.5), not a name.")


def ask_addresses(who, current_lan="", current_public=""):
    """(LAN address, internet address or ""), checked with the host."""
    heading("Question 2 of 2: Is this the right address?")
    lan = lan_address() or current_lan
    if lan:
        say("This computer's address on your network is %s." % lan)
    else:
        say("Setup could not find this computer's address on your network.")
    if who == "home" or not lan:
        lan = ask_ip("Address on your network", lan)
    if who == "home":
        return lan, ""
    public = public_address() or current_public
    if public:
        say("Your internet address is %s. Friends' consoles use it." % public)
    else:
        say("Setup could not find your internet address. Type it.")
    say("The game takes an IP address, not a name, so if your internet provider changes it,")
    say("run %s again and choose Change who plays." % PROGRAM)
    return lan, ask_ip("Internet address", public)


# --- steps -------------------------------------------------------------------------------

def failed(what, tail):
    say()
    say(what)
    reason = proglib.explain(tail)
    if reason:
        say(reason)
    say("The last lines it wrote:")
    for line in tail[-12:]:
        say("  " + line)
    say("Everything it wrote is in %s" % LOG)


def build_image(progress, log):
    progress.begin("Getting the server program ready", "about 2 minutes the first time")
    code, tail = proglib.run_logged(["docker", "compose", "build"], log, cwd=ROOT, progress=progress)
    progress.end(code == 0)
    if code != 0:
        failed("Getting the server program ready failed.", tail)
    return code == 0


def start_server(progress, log):
    ensure_network()
    progress.begin("Starting the server", "under a minute")
    code, tail = proglib.run_logged(["docker", "compose", "up", "-d"], log, cwd=ROOT, progress=progress)
    ok = code == 0 and wait_ready(quiet=True)
    progress.end(ok)
    if code != 0:
        failed("Starting the server failed.", tail)
        return False
    if not ok:
        return False
    progress.begin("Checking the server", "a few seconds")
    ok, message = smokelib.check()
    log.write(message + "\n")
    progress.end(ok)
    if not ok:
        say(message)
    return ok


def setup_dns(env, choices, quiet=False):
    """Make sure a resolver runs here and has this game's names. True when it does."""
    container = dnslib.find()
    if container is None:
        values = dnslib.settings(who_plays(env, choices), choices.get("lan", ""), choices.get("public", ""),
                                 bool(choices.get("desktop")))
        if not quiet:
            say("Starting the DNS server players' consoles use...")
        ok, out = dnslib.start(ROOT, values)
        if not ok:
            say("The DNS server did not start:")
            say(out[-1500:])
            if "53" in out and ("in use" in out or "allocated" in out or "not available" in out):
                say("Another program on this computer answers DNS on port 53. Stop it, or run the")
                say("game servers on a computer where port 53 is free.")
            choices["dns"] = "failed"
            write_choices(choices)
            return False
        for _ in range(10):
            container = dnslib.find()
            if container:
                break
            time.sleep(1)
    if container is None:
        say("The DNS server is not running.")
        choices["dns"] = "failed"
        write_choices(choices)
        return False
    status, out = dnslib.add_names(container)
    own = dnslib.ours(container)
    if status == "ok":
        if not quiet:
            say("The DNS server (%s) answers the game's names with this computer's address." % container)
        choices["dns"] = "ours" if own else "shared"
    elif status == "old":
        say("The DNS server on this computer (%s) cannot take the game's names: the OpenLobby" % container)
        say("image is too old for this, update it. Then choose Set up DNS again in the menu.")
        choices["dns"] = "old"
    else:
        say("Adding the game's names to the DNS server failed:")
        say(out[-1500:])
        choices["dns"] = "failed"
    write_choices(choices)
    return status == "ok"


def share_port_80(quiet=False):
    """Pass stock installs' port 80 requests for the rankings to this server, through the
    machine's shared web server (started when there is none and port 80 is free)."""
    container = httplib.find()
    if not container:
        ensure_network()
        status, out = httplib.start(ROOT)
        if status == "busy":
            say("Another program on this computer uses port 80, so installs without the ranking")
            say("patch will not see the rankings. Patched installs use port 5732 and are fine.")
            return "busy"
        if status != "ok":
            say("The web server on port 80 did not start (%s)." % out[-300:])
            return "failed"
        for _ in range(10):
            container = httplib.find()
            if container:
                break
            time.sleep(1)
        if not container:
            say("The web server on port 80 is not running.")
            return "failed"
    status, out = httplib.add_hosts(container)
    if status == "ok" and not quiet:
        say("The web server on port 80 (%s) passes the ranking pages on to this server." % container)
    elif status == "old":
        say("The web server on port 80 (%s) cannot pass the ranking host on: the OpenLobby image" % container)
        say("is too old for this, update it. Then choose Set up DNS again in the menu.")
    elif status == "failed":
        say("The web server on port 80 did not take the ranking host (%s)." % out[-300:])
    return status


def open_router_ports(choices, quiet=False):
    """Ask the router to forward the ports. Returns (all open?, the router's
    internet address or "")."""
    lan = lan_address()
    ports = ports_to_open(choices.get("dns") == "ours")
    if not quiet:
        say("Asking your router to open the ports...")
    gateway = upnp.find_gateway(lan) if lan else None
    if not gateway:
        if not quiet:
            say("Your router did not answer (its UPnP setting may be off). The ports need opening by hand.")
        return False, ""
    results = upnp.open_ports(gateway, ports, lan)
    try:
        external = gateway.external_ip()
    except upnp.UpnpError:
        external = ""
    good = [p for p, r in results.items() if r in ("opened", "already")]
    bad = {p: r for p, r in results.items() if p not in good}
    if not quiet or bad:
        if not bad:
            say("Your router opened the ports for this computer (%s)." % lan)
        else:
            say("Your router opened %d of %d ports. Not opened:" % (len(good), len(ports)))
            for (port, protocol), r in bad.items():
                say("  %d %s: %s" % (port, protocol, "another device uses it" if r == "taken" else r))
    if external and upnp.is_private(external):
        say()
        say("Note: your router has no internet address of its own (%s). Your internet provider" % external)
        say("probably shares one address between customers, or there is a second router in front")
        say("of this one. Friends outside your home will likely not reach the server. Ask your")
        say("provider for a public IP address.")
    return not bad, external


def show_port_help(choices, router=""):
    lan = lan_address() or "this computer"
    heading("Open these ports on your router")
    say("Your router needs to send these ports to this computer (%s)." % lan)
    say("Open the router's settings page (usually http://%s, or the address printed on" % (router or "192.168.0.1"))
    say('the router) and look for "Port forwarding" (or "Virtual server", or "NAT").')
    say()
    say("  Ports            Type   To")
    for first, last in port_ranges(GAME_PORTS):
        shown = str(first) if first == last else "%d-%d" % (first, last)
        say("  %-16s TCP    %s" % (shown, lan))
    if choices.get("dns") == "ours":
        say("  %-16s UDP    %s" % ("53", lan))
    say()
    say("Steps for many router models: %s" % PORT_HELP)


def router_step(env, choices):
    if who_plays(env, choices) != "internet":
        return
    heading("Router")
    ok, router = open_router_ports(choices)
    choices["router"] = "open" if ok else "closed"
    write_choices(choices)
    if not ok:
        show_port_help(choices, router)


def pair_panel(env, choices, asked=False):
    """Offer to manage the server from an OpenLobby panel running here."""
    url = panellib.find()
    if not url:
        if asked:
            say("No OpenLobby admin panel is running on this computer.")
        return False
    if not asked:
        say()
        if not ask_yes("An OpenLobby admin panel is running here (%s). Manage pop'n from it?" % url):
            return False
    if not env.get("POPN_ADMIN_TOKEN"):
        write_env({"POPN_ADMIN_TOKEN": new_token()})
        env = read_env()
        compose("up", "-d", "gate", check=False, capture=True)
        wait_ready(quiet=True)
    say("In the panel, open Settings, Modules, Pair with a setup program. It shows a code.")
    while True:
        code = ask("The code (Enter to skip)")
        if not code:
            return False
        secret, message = panellib.pair(url, code, KEY, TITLE, ADMIN_URL, env["POPN_ADMIN_TOKEN"])
        say(message)
        if secret:
            write_env({"OPENLOBBY_PAIRING": secret})
            choices["panel"] = url
            write_choices(choices)
            return True


# --- finishing ---------------------------------------------------------------------------

def finish(env, choices):
    address = players_dns(env, choices)
    say()
    say("=" * 72)
    say("Your server is running.")
    say()
    if choices.get("dns") not in ("ours", "shared"):
        say("The DNS server is not set up yet, so players cannot connect. Choose Set up DNS")
        say("again in the menu once the reason above is fixed.")
        say()
    say("Players need a game install whose network settings and DNS point at this server.")
    if who_plays(env, choices) == "internet":
        say()
        if choices.get("router") == "open":
            say("Your router has the ports open.")
        else:
            say("Friends can join once the ports above are open on your router.")
        if choices.get("dns") == "shared":
            say("The DNS server here belongs to another install. Friends over the internet can use it")
            say("only if it answers internet clients (POL_DNS_PUBLIC=1 in OpenLobby's settings).")
        if choices.get("desktop") and choices.get("lan"):
            say("Players on your home network use %s too; if they cannot connect, your router" % address)
            say("does not pass its own internet address back in. They can type %s instead." % choices["lan"])
        say("Setup cannot test the internet side from inside your home. Ask a friend to try.")
    say()
    say("The server keeps running when you close this window, as long as Docker runs.")
    say("Run %s again to stop it, change settings or make backups." % PROGRAM)
    say("=" * 72)
    say("Tell your players: set DNS 1 to %s in the game's network settings" % address)


def first_run(dry_run=False):
    say(PROGRAM)
    say("=" * len(PROGRAM))
    say("This sets up a pop'n Taisen Puzzle-dama Online server on this computer. Answer two")
    say("questions, then setup does the rest in a few minutes.")
    env = read_env(ENV_EXAMPLE)
    choices = read_choices()
    who = ask_who()
    lan, public = ask_addresses(who)
    desktop = docker_desktop() if not dry_run else sys.platform in ("win32", "darwin")
    changes = first_settings(who, lan, public, env, desktop)
    choices.update(who=who, lan=lan, public=public, desktop=desktop)

    heading("Ready")
    say("  Players:  %s" % ("people on your home network" if who == "home" else "friends over the internet"))
    say("  Address:  %s%s" % (changes["POPN_ADVERTISE"], " (players on your network: %s)" % lan
                                if changes["POPN_ADVERTISE_MAP"] else ""))
    say("Everything else uses the usual settings. You can change them later in %s." % ENV)
    if dry_run:
        say()
        say("Dry run: nothing was written. Setup would write these settings to %s:" % ENV)
        for key, value in changes.items():
            if key != "POPN_ADMIN_TOKEN":
                say("  %s=%s" % (key, value))
        say("  POPN_ADMIN_TOKEN: made at random")
        say("It would also start the DNS server with: %s" % ", ".join(
            "%s=%s" % kv for kv in dnslib.settings(who, lan, public, desktop).items() if kv[1]))
        return
    if not ask_yes("Start?"):
        return
    write_env(changes)
    env = read_env()
    write_choices(choices)
    say()
    say("Details of every step go to %s" % LOG)
    complete_setup(env, choices)


def complete_setup(env, choices):
    """Build, start, set up DNS and the router, and offer the panel."""
    heading("Starting")
    progress = proglib.Progress()
    with log_file() as log:
        if not build_image(progress, log):
            return False
        if not start_server(progress, log):
            return False
    setup_dns(env, choices)
    share_port_80()
    router_step(env, choices)
    pair_panel(read_env(), choices)
    finish(read_env(), read_choices())
    return True


# --- menu --------------------------------------------------------------------------------

def start_server_menu():
    if server_running():
        say("The server is already running.")
        return
    env, choices = read_env(), read_choices()
    if who_plays(env, choices) == "internet":
        open_router_ports(choices, quiet=True)
    ensure_network()
    compose("up", "-d")
    if wait_ready():
        smoke()


def stop_server():
    if not server_running():
        say("The server is not running.")
        return
    if ask_yes("Stop the server? Players in the game are disconnected.", False):
        compose("stop")
        say("Stopped. It starts again when you choose Start the server.")


def change_who():
    env, choices = read_env(), read_choices()
    who = ask_who(who_plays(env, choices))
    lan, public = ask_addresses(who, choices.get("lan", ""), choices.get("public", ""))
    desktop = docker_desktop()
    write_env(advertise_settings(who, lan, public, desktop))
    choices.update(who=who, lan=lan, public=public, desktop=desktop)
    write_choices(choices)
    if server_running():
        say("Restarting the server with the new address...")
        compose("up", "-d")
        if wait_ready():
            smoke()
    container = dnslib.find()
    if container and dnslib.ours(container):
        say("Restarting the DNS server with the new address...")
        dnslib.start(ROOT, dnslib.settings(who, lan, public, desktop))
    router_step(read_env(), choices)
    say("Tell your players: set DNS 1 to %s" % players_dns(read_env(), choices))


def update():
    """Fetch the new version and rebuild the server program."""
    if os.path.exists(os.path.join(ROOT, ".git")):
        if run(["git", "pull", "--ff-only"], check=False).returncode != 0:
            say("git pull failed (local changes?). Nothing was changed.")
            return
    else:
        release = latest_release()
        if not release:
            say("Could not reach %s. Check the internet connection." % RELEASES)
            return
        tag, url = release
        if tag == installed_version():
            say("This is the newest version (%s)." % tag)
            if not ask_yes("Rebuild the server program anyway?", False):
                return
        else:
            say("Downloading %s..." % tag)
            archive = os.path.join(ROOT, "logs", "update.zip")
            os.makedirs(os.path.dirname(archive), exist_ok=True)
            try:
                urllib.request.urlretrieve(url, archive)
                unpack_server(archive, ROOT, tag)
            except (OSError, zipfile.BadZipFile) as e:
                say("The download failed (%s). Nothing was changed." % e)
                return
            finally:
                if os.path.exists(archive):
                    os.remove(archive)
            say("Updated the server's files to %s." % tag)
            if FROZEN:
                say("A new %s is also on %s" % (PROGRAM, RELEASES))
    progress = proglib.Progress()
    with log_file() as log:
        if not build_image(progress, log):
            return
        start_server(progress, log)


def backup():
    env = read_env()
    data = os.path.join(state_dir(env), "data")
    if not os.path.isdir(data):
        say("There is nothing to back up yet (%s is missing)." % data)
        return
    os.makedirs(BACKUPS, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = shutil.make_archive(os.path.join(BACKUPS, "popn-%s" % stamp), "zip", data)
    say("Saved %s" % archive)
    say("It holds every account and character. Keep a copy of it and of %s somewhere safe." % ENV)


def restore():
    zips = sorted(f for f in os.listdir(BACKUPS) if f.endswith(".zip")) if os.path.isdir(BACKUPS) else []
    if not zips:
        say("No backups in %s." % BACKUPS)
        return
    for i, name in enumerate(zips, 1):
        say("  %d. %s" % (i, name))
    pick = ask("Restore which one (number, Enter to cancel)")
    if not pick.isdigit() or not 1 <= int(pick) <= len(zips):
        return
    say("This replaces every account and character with the ones in %s." % zips[int(pick) - 1])
    say("Play since then is lost.")
    if not ask_yes("Go on?", False):
        return
    running = server_running()
    compose("stop", check=False)
    data = os.path.join(state_dir(read_env()), "data")
    os.makedirs(data, exist_ok=True)
    with zipfile.ZipFile(os.path.join(BACKUPS, zips[int(pick) - 1])) as z:
        for name in z.namelist():
            target = os.path.normpath(os.path.join(data, name))
            if target.startswith(os.path.normpath(data) + os.sep):
                z.extract(name, data)
    say("Restored.")
    if running:
        compose("up", "-d")
        if wait_ready():
            smoke()


def logs():
    say("Showing what the server is doing. Press Ctrl+C to go back to the menu.")
    try:
        compose("logs", "-f", "--tail", "200", check=False)
    except KeyboardInterrupt:
        pass


def router_again():
    env, choices = read_env(), read_choices()
    if who_plays(env, choices) != "internet":
        say("Your server is set up for your home network only, so the router needs no open ports.")
        say("Choose Change who plays to change that.")
        return
    router_step(env, choices)


def dns_again():
    setup_dns(read_env(), read_choices())
    share_port_80()


def pair_again():
    pair_panel(read_env(), read_choices(), asked=True)


def change_token():
    say("The admin token lets an OpenLobby panel run this server. A new one stops the old one")
    say("from working.")
    if not ask_yes("Make a new token?", False):
        return
    token = new_token()
    write_env({"POPN_ADMIN_TOKEN": token})
    if server_running():
        compose("up", "-d", "gate")
        wait_ready(quiet=True)
    env, choices = read_env(), read_choices()
    url = panellib.find() or choices.get("panel")
    if env.get("OPENLOBBY_PAIRING") and url:
        ok, message = panellib.update(url, KEY, env["OPENLOBBY_PAIRING"], token, ADMIN_URL)
        say(message)
    else:
        say("Saved. A panel that is not paired needs POL_ADMIN_GAME_POPN_URL=%s and" % ADMIN_URL)
        say("POL_ADMIN_GAME_POPN_TOKEN set to POPN_ADMIN_TOKEN from %s." % ENV)


def tell_players():
    finish(read_env(), read_choices())


def remove():
    say("This stops the server and takes the game's names out of the DNS server. Accounts,")
    say("characters and settings stay in %s." % ROOT)
    if not ask_yes("Remove the server?", False):
        return
    compose("down", check=False)
    container = dnslib.find()
    if container:
        dnslib.remove_names(container)
    container = httplib.find()
    if container:
        httplib.remove_hosts(container)
    say("Removed. Run %s again to set it up again." % PROGRAM)


MENU = [("Start the server", start_server_menu),
        ("Stop the server", stop_server),
        ("Change who plays (and the address)", change_who),
        ("Update", update),
        ("Make a backup", backup),
        ("Restore a backup", restore),
        ("Show what the server is doing (logs)", logs),
        ("Check that the server works", smoke),
        ("Open the router ports again", router_again),
        ("Set up DNS again", dns_again),
        ("Manage from an OpenLobby admin panel", pair_again),
        ("Make a new admin token", change_token),
        ("Show what to tell players", tell_players),
        ("Remove the server", remove)]


def menu():
    while True:
        env, choices = read_env(), read_choices()
        state = "running" if server_running() else "stopped"
        heading("pop'n server: %s (players set DNS 1 to %s)" % (state, players_dns(env, choices) or "?"))
        for i, (title, _) in enumerate(MENU, 1):
            say("  %2d. %s" % (i, title))
        say("   0. Quit")
        pick = ask("Type a number", "0")
        if pick == "0":
            return
        if pick.isdigit() and 1 <= int(pick) <= len(MENU):
            try:
                MENU[int(pick) - 1][1]()
            except KeyboardInterrupt:
                say()
            except SystemExit as e:
                say(str(e))


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog=PROGRAM, description=(
        "Sets up and runs a pop'n Taisen Puzzle-dama Online server in Docker. The first run asks "
        "two questions; later runs show a menu."))
    ap.add_argument("--folder", help="the server folder (default: the one this runs from, or "
                                     "'%s' in your home folder for the setup program)" % DEFAULT_FOLDER)
    ap.add_argument("--dry-run", action="store_true",
                    help="ask the questions, then show what would be done; writes nothing and needs no Docker")
    return ap.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    use_root(find_root(args.folder))
    try:
        if args.dry_run:
            say("Server folder: %s%s" % (ROOT, "" if os.path.exists(os.path.join(ROOT, "compose.yml")) else
                                         " (the server's files would be unpacked here)"))
            say("Docker: %s" % docker_state())
            first_run(dry_run=True)
            return
        if not ensure_server_files():
            return
        os.chdir(ROOT)
        wait_for_docker()
        if not os.path.exists(ENV):
            first_run()
            return
        menu()
    except KeyboardInterrupt:
        say()
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    finally:
        if FROZEN:
            # A double-clicked program's window closes when it ends: keep it open to be read.
            try:
                input("Press Enter to close this window.")
            except (EOFError, KeyboardInterrupt):
                pass
