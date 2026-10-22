"""gamelink.py -- the loopback account API the OpenLobby website calls to link a game login to an
OpenLobby account and to reset its password from the website.

Two locks: the peer must be loopback (the website runs on this host), and X-Signature =
HMAC-SHA256(GAMELINK_SECRET, X-Timestamp + "\\n" + body), with the timestamp inside GAMELINK_SKEW
seconds. No secret = the API does not start. Requests and replies are JSON; passwords are never
logged.

  POST /v1/verify        {"account", "password"} -> {"ok": true, "account", "key"}
                                                   | {"ok": false, "error": "no_account"|"bad_password"}
  POST /v1/set-password  {"account", "password"} -> {"ok": true} | {"ok": false, "error": ...}

`account` is the name the player types in the game; the reply's `account` is the stored spelling and
`key` the game's own stable id for it (pop'n: the login id itself). The website keeps the link by
key. pop'n adds "no_password": the id exists but the server holds no password for it (an open-mode
login), so nothing can be checked; the website lets the player claim it by setting one.
"""
import hashlib
import hmac
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRET = os.environ.get("GAMELINK_SECRET", "").encode()
SKEW = int(os.environ.get("GAMELINK_SKEW", "60"))
ALLOW = {"127.0.0.1", "::1"}
MAX_BODY = 4096


def signature(secret, ts, body):
    return hmac.new(secret, ts.encode() + b"\n" + body, hashlib.sha256).hexdigest()


def serve(routes, port, log=print):
    """Start the API on 127.0.0.1:port in a daemon thread. routes = {"/v1/x": fn(req) -> dict}.
    Returns the server, or None when GAMELINK_SECRET is unset."""
    if len(SECRET) < 32:
        log("gamelink: GAMELINK_SECRET unset or shorter than 32 characters, account API off")
        return None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, code, obj=None):
            out = json.dumps(obj or {}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def do_POST(self):
            if self.client_address[0] not in ALLOW:
                return self._send(403)
            fn = routes.get(self.path)
            if fn is None:
                return self._send(404)
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 < n <= MAX_BODY:
                return self._send(400)
            body = self.rfile.read(n)
            ts = self.headers.get("X-Timestamp") or ""
            try:
                fresh = abs(time.time() - int(ts)) <= SKEW
            except ValueError:
                fresh = False
            if not fresh or not hmac.compare_digest(signature(SECRET, ts, body),
                                                    self.headers.get("X-Signature") or ""):
                log("gamelink: refused %s %s: bad signature or stale timestamp"
                    % (self.client_address[0], self.path))
                return self._send(401)
            try:
                req = json.loads(body)
            except ValueError:
                return self._send(400)
            if not isinstance(req, dict):
                return self._send(400)
            res = fn(req)
            log("gamelink: %s %s -> %s" % (self.path, req.get("account", ""),
                                          "ok" if res.get("ok") else res.get("error")))
            return self._send(200, res)

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("gamelink: account API on 127.0.0.1:%d" % port)
    return srv
