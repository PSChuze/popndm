# SPDX-License-Identifier: AGPL-3.0-or-later OR GPL-3.0-or-later
# Copyright (C) OpenLobby contributors. This file alone may be used under either licence,
# so a GPL-3.0 game server can carry a copy of it unchanged. See LICENSE for the AGPL.
"""OpenLobby admin port: drop this one file into a game server, and an
OpenLobby admin panel can show the game and run its actions.

The panel side is docs/admin-modules.md ("The game describes itself"). This
file is the game side: a small HTTP server, standard library only (Python
3.8+), that answers

    GET  /openlobby/module.json     what the game is
    GET  /openlobby/status.json     who is playing now
    GET  /openlobby/calendar.json   what is scheduled (optional; ?from=&to= unix seconds)
    GET  /openlobby/help.json       players' help requests (optional; with help/thread.json,
                                    POST help/reply and help/close)
    GET  /openlobby/player.json     what the game knows of one account (optional;
                                    ?polid=&handles=&members=)
    GET  /openlobby/players.json    find players by name or id (optional; ?q=&limit=)
    POST /openlobby/actions/<id>    run one action

and, if the game has them, its own tool pages under any other path.

    import olport

    port = olport.AdminPort("kart", "Kart Club", platform="PS2 HDD",
                            token=os.environ["KART_ADMIN_TOKEN"], bind="0.0.0.0", port=8820)

    @port.status
    def status():
        return {"online": len(players), "players": [{"name": p.name, "room": p.room} for p in players]}

    @port.action("banner", "Banner message", kind="announce",
                 fields=[{"id": "text", "label": "Message"}])
    def banner(params, actor):
        lobby.show_banner(params.get("text", ""))
        return True, "Shown in every lobby."

    port.start()          # a daemon thread; port.stop() ends it

A game with a schedule can show it in the panel's calendar:

    @port.calendar
    def calendar(lo, hi):
        return [{"id": "gp-20261031", "title": "Halloween Grand Prix", "start": 1793404800,
                 "end": 1793491200, "kind": "cup", "about": "Pumpkin karts only."}]

Entries touching [lo, hi) in unix seconds; "times" ([[start, end], ...]) may list the
sessions inside a long one.

Players' help requests go to the panel's Inbox with port.help_desk(list, thread, reply,
close): list(open_only) -> [{"id", "subject", "who", "at", "closed", "waiting", "count"}],
thread(id) -> {"id", "subject", "closed", "who": {"name", "online"}, "messages":
[{"from": "player" or "staff", "subject", "text", "at"}]} (None when there is no such id),
reply(id, text, close, actor) and close(id, actor) -> (ok, message).

The account window asks every game about one player with @port.player: fn(who) gets
{"polid", "handles": [...], "members": [...], "links": {account system: [{"key", "name"}]}}
(links: logins on this game's server the account's members have proved are theirs, the reliable
way to find a player who signs in to the game with its own login) and returns {"characters": [{"name", "detail",
"online", "facts": [{"label", "value"}]}], "note"}, or None for a player the game never saw.

The panel's player picker (an action field of "type": "player", and the account boxes) asks
@port.players: fn(q, limit) gets the typed text (maybe empty) and 1-50, and returns
[{"id", "label", "detail", "online": True, False or None}], best first: a case-insensitive
match on name or id, players online now before the rest. "id" is what the action field gets.

An action gets the posted fields (strings) and the name of the panel user who
asked, and returns (ok, message). One that raises answers {"ok": false} with
the exception's type, never its text. A kind of "announce" is used by the
panel's maintenance warning, "kick" by an account's Kick (sent {"polid",
"handles"}).

The token is checked on every request (X-Devtool-Token). Bound anywhere but
loopback, the port refuses to start without one.
"""
import hmac
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote_plus as unquote

PREFIX = "/openlobby/"
MAX_BODY = 1 << 20
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_KEY = re.compile(r"^[a-z0-9]{1,16}$")
LOOPBACK = ("127.0.0.1", "::1", "localhost")


def _links(raw):
    """The player lookup's links parameter: {system: [{"key", "name"}]}, or {} for anything else."""
    try:
        got = json.loads(raw or "{}")
    except ValueError:
        return {}
    if not isinstance(got, dict):
        return {}
    return {str(k): [{"key": str(x.get("key", "")), "name": str(x.get("name", ""))}
                     for x in v if isinstance(x, dict)]
            for k, v in got.items() if isinstance(v, list)}


class AdminPort:
    def __init__(self, key, title, short="", platform="", about="", token="",
                 bind="127.0.0.1", port=0, content_ids=(), services=(), accounts=""):
        if not _KEY.match(key or ""):
            raise ValueError("key must be 1-16 of a-z and 0-9")
        if bind not in LOOPBACK and not token:
            raise ValueError("a port bound to %s needs a token" % bind)
        self.key, self.title, self.short = key, title, short
        self.platform, self.about = platform, about
        self.token, self.bind, self.port = token or "", bind, int(port)
        self.content_ids = list(content_ids)
        #: the account system its logins belong to (the key in the player lookup's links and
        #: the panel's People links); blank = the game key
        self.accounts = accounts or ""
        #: [{"name", "containers": [...], "ports": {"7000": "game"}, "warning"}]
        self.services = list(services)
        self._status = None
        self._calendar = None
        self._help = None
        self._player = None
        self._players = None
        self._actions = {}          # id -> (meta, fn)
        self._tools = []            # (meta, fn), in order
        self._srv = None

    # -- what the game offers ------------------------------------------------ #
    def status(self, fn):
        """Decorator: `fn()` returns the status document (docs/admin-modules.md)."""
        self._status = fn
        return fn

    def calendar(self, fn):
        """Decorator: `fn(lo, hi)` returns the entries touching [lo, hi) (unix seconds)."""
        self._calendar = fn
        return fn

    def help_desk(self, list, thread, reply, close):
        """Players' help requests (see above)."""
        self._help = (list, thread, reply, close)

    def player(self, fn):
        """Decorator: `fn(who)` -> what the game knows of one account, or None."""
        self._player = fn
        return fn

    def players(self, fn):
        """Decorator: `fn(q, limit)` -> [{"id", "label", "detail", "online"}] for the picker."""
        self._players = fn
        return fn

    def action(self, id, label, kind="", fields=(), about="", danger=False):
        """Decorator: `fn(params, actor)` -> (ok, message)."""
        if not _ID.match(id):
            raise ValueError("action id %r does not fit" % id)

        def wrap(fn):
            self._actions[id] = ({"id": id, "label": label, "kind": kind, "about": about,
                                  "danger": bool(danger), "fields": list(fields)}, fn)
            return fn
        return wrap

    def tool(self, id, label, path, about=""):
        """Decorator: a page of the game's own, served at /<path> and below.
        `fn(method, subpath, query, body, actor)` -> (status, content_type, bytes)."""
        if not _ID.match(id) or path.startswith("openlobby") or ".." in path:
            raise ValueError("tool %r / %r does not fit" % (id, path))

        def wrap(fn):
            self._tools.append(({"id": id, "label": label, "path": path, "about": about}, fn))
            return fn
        return wrap

    def manifest(self):
        return {"v": 1, "key": self.key, "title": self.title, "short": self.short,
                "platform": self.platform, "about": self.about,
                "content_ids": self.content_ids, "services": self.services,
                "accounts": self.accounts or self.key,
                "calendar": self._calendar is not None,
                "help": self._help is not None, "player": self._player is not None,
                "players": self._players is not None,
                "tools": [m for m, _fn in self._tools],
                "actions": [m for m, _fn in self._actions.values()]}

    # -- running ------------------------------------------------------------- #
    def start(self):
        """Listen on (bind, port) in a daemon thread. -> (host, port)."""
        port = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "olport"

            def log_message(self, *a):
                pass

            def _send(self, status, body, ctype="application/json; charset=utf-8"):
                if not isinstance(body, (bytes, bytearray)):
                    body = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _allowed(self):
                got = self.headers.get("X-Devtool-Token", "")
                if port.token and not hmac.compare_digest(got.encode(), port.token.encode()):
                    self._send(403, {"error": "bad or missing token"})
                    return False
                return True

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n > MAX_BODY:
                    raise ValueError("too large")
                return self.rfile.read(n) if n else b""

            def _actor(self):
                return (self.headers.get("X-Devtool-Actor") or "")[:64]

            def do_GET(self):
                self._route("GET")

            def _help_route(self, sub, method, q, body):
                lst, thread, reply, close = port._help
                try:
                    if sub == "help.json" and method == "GET":
                        return self._send(200, {"threads": list(lst(q.get("open") == "1") or [])})
                    if sub == "help/thread.json" and method == "GET":
                        t = thread(unquote(q.get("id", "")))
                        return self._send(200, t) if t else self._send(404, {"error": "no such request"})
                    if sub in ("help/reply", "help/close") and method == "POST":
                        try:
                            p = json.loads(body or b"{}")
                        except ValueError:
                            return self._send(400, {"ok": False, "msg": "the body is not JSON"})
                        p = p if isinstance(p, dict) else {}
                        tid = str(p.get("id") or "")
                        if sub == "help/reply":
                            ok, msg = reply(tid, str(p.get("text") or ""), bool(p.get("close")), self._actor())
                        else:
                            ok, msg = close(tid, self._actor())
                        return self._send(200, {"ok": bool(ok), "msg": str(msg)[:500]})
                except Exception as exc:                # noqa: BLE001 -- say what kind, not what
                    return self._send(500, {"ok": False, "msg": "failed: %s" % exc.__class__.__name__})
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                self._route("POST")

            def _route(self, method):
                if not self._allowed():
                    return
                path, _, query = self.path.partition("?")
                try:
                    body = self._body() if method == "POST" else b""
                except ValueError:
                    return self._send(413, {"error": "too large"})
                q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                if path == PREFIX + "module.json" and method == "GET":
                    return self._send(200, port.manifest())
                if path == PREFIX + "status.json" and method == "GET":
                    if port._status is None:
                        return self._send(200, {"up": True})
                    try:
                        return self._send(200, port._status())
                    except Exception as exc:            # noqa: BLE001 -- say what kind, not what
                        return self._send(200, {"up": None,
                                                "error": "status failed: %s" % exc.__class__.__name__})
                if path == PREFIX + "calendar.json" and method == "GET" and port._calendar is not None:
                    try:
                        lo, hi = int(q.get("from", "0")), int(q.get("to", "0"))
                    except ValueError:
                        return self._send(400, {"error": "from and to are unix seconds"})
                    try:
                        return self._send(200, {"events": list(port._calendar(lo, hi) or [])})
                    except Exception as exc:            # noqa: BLE001
                        return self._send(500, {"error": "calendar failed: %s" % exc.__class__.__name__})
                if path.startswith(PREFIX + "help") and port._help is not None:
                    return self._help_route(path[len(PREFIX):], method, q, body)
                if path == PREFIX + "player.json" and method == "GET" and port._player is not None:
                    who = {"polid": unquote(q.get("polid", "")),
                           # the panel encodes the commas too (urlencode), so unquote first
                           "handles": [h for h in unquote(q.get("handles", "")).split(",") if h],
                           "members": [x for x in unquote(q.get("members", "")).split(",") if x],
                           "links": _links(unquote(q.get("links", "")))}
                    try:
                        return self._send(200, port._player(who) or {})
                    except Exception as exc:            # noqa: BLE001
                        return self._send(500, {"error": "player failed: %s" % exc.__class__.__name__})
                if path == PREFIX + "players.json" and method == "GET" and port._players is not None:
                    try:
                        limit = int(q.get("limit") or "20")
                    except ValueError:
                        limit = 0
                    if not 1 <= limit <= 50:
                        return self._send(400, {"error": "limit is 1 to 50"})
                    try:
                        got = list(port._players(unquote(q.get("q", "")), limit) or [])[:limit]
                    except Exception as exc:            # noqa: BLE001
                        return self._send(500, {"error": "players failed: %s" % exc.__class__.__name__})
                    return self._send(200, {"players": got})
                if path.startswith(PREFIX + "actions/") and method == "POST":
                    hit = port._actions.get(path[len(PREFIX + "actions/"):])
                    if hit is None:
                        return self._send(404, {"ok": False, "msg": "no such action"})
                    try:
                        params = json.loads(body or b"{}")
                    except ValueError:
                        return self._send(400, {"ok": False, "msg": "the body is not JSON"})
                    params = {str(k): v for k, v in params.items()} if isinstance(params, dict) else {}
                    try:
                        ok, msg = hit[1](params, self._actor())
                    except Exception as exc:            # noqa: BLE001
                        ok, msg = False, "failed: %s" % exc.__class__.__name__
                    return self._send(200, {"ok": bool(ok), "msg": str(msg)[:500]})
                if path.startswith(PREFIX):
                    return self._send(404, {"error": "not found"})
                sub = path.lstrip("/")
                best = None
                for meta, fn in port._tools:
                    p = meta["path"]
                    if p == "" or sub == p or sub.startswith(p + "/") or sub.startswith(p + "."):
                        if best is None or len(p) > len(best[0]["path"]):
                            best = (meta, fn)
                if best is None:
                    return self._send(404, {"error": "not found"})
                try:
                    status, ctype, data = best[1](method, sub, query, body, self._actor())
                except Exception as exc:                # noqa: BLE001
                    return self._send(500, {"error": "tool failed: %s" % exc.__class__.__name__})
                return self._send(status, data, ctype)

        self._srv = ThreadingHTTPServer((self.bind, self.port), Handler)
        self._srv.daemon_threads = True
        threading.Thread(target=self._srv.serve_forever, daemon=True,
                         name="olport-%s" % self.key).start()
        return self._srv.server_address[:2]

    def stop(self):
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
