"""The pop'n server on tcp/5731 (ps2pzd1.konamionline.com): gate, account and lobby.

The protocol in short:

- The client makes three connections. GATE: 0x2000 -> 0x2002 server list (type 1 =
  account, type 0 = lobby; both point back here). ACCOUNT: login, 0x4005 new account,
  0x451d, characters. LOBBY: login again, 0x4106 select character, 0x4202 enter lobby,
  then every lobby / room / match message. One process serves all three.
- The login id/password (0x4007), new characters (0x4100) and issued accounts (0x4006)
  are Blowfish-enciphered (bf.py). Accounts are keyed on the plaintext login id.
- The uid in every lobby/room/match message is the SELECTED CHARACTER ID.
- Battles are peer-to-peer on TCP 5730. The server never relays gameplay: it pairs the
  two START presses (0x4500), hands each player the other's IPv4 (0x4409 -> 0x440b),
  and frames the match (0x4517/0x4519/0x4508). At least one console must accept
  inbound TCP 5730.
- Almost every request arms a ~15 s client timer that only its specific reply clears;
  a missing or malformed reply is the fatal "-9999". Pushes never clear it.

Server-to-client serials start at 1 and must increase by exactly 1 per frame (the client
checks them, error -0x49). Sessions push to each other from other threads, so every
send goes through the session's write lock.

Env: POPN_BIND (0.0.0.0), POPN_PORT (5731), POPN_LOG_DIR (./data/logs),
     POPN_STATE_DB (./data/accounts.json), POPN_AUTH_DB (JSON {id: password}), POPN_AUTH_OPEN=1
     (accept unknown ids; known passwords are still checked), POPN_ADVERTISE /
     POPN_ADVERTISE_MAP (the IP in the 0x2002 server list, per source), POPN_SVR_PORT (the port
     in the server list), POPN_P2P_ADDR_MAP ('seen=give,...': rewrite the opponent address
     handed out in 0x440b), POPN_IDLE_TIMEOUT (s, 120), POPN_P2P_RELAY=1 / POPN_RELAY_PORT
     (5730) / POPN_RELAY_HOST: battle relay for relay-patched clients (relay.py; the relay
     patch), POPN_LIVE_FILE / POPN_LIVE_EVERY (s, 10; 0 = off): the boards' snapshot (live.py),
     POPN_ADMIN_PORT / _BIND / _TOKEN: an OpenLobby admin panel (adminport.py, off unless set),
     POPN_FAKE_CHAR=1, POPN_SEED_ROOM=1 / POPN_SEED_NAME (test knobs).
"""
import datetime
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import threading
import time

from . import adminport
from . import bf
from . import gamelink
from . import live
from . import model
from . import relay
from . import wire

HELLO, CHALLENGE, LOGIN, GOODBYE = 0x2000, 0x4003, 0x4007, wire.GOODBYE
SVRLIST_REPLY, LOGIN_REPLY = 0x2002, 0x4008

# Negative u32 statuses the client shows with its own text.
ERR_LOGIN = -240           # wrong ID or password
ERR_LOGGED_IN = -242       # already logged in
ERR_NO_ROOM = -500         # the room doesn't exist
ERR_ROOM_FULL = -0x1F7     # the room is full
ERR_ROOM_PW = -0x1EE       # wrong room password
ERR_NOT_OWNER = -0x38B     # you aren't the room's creator
ERR_IN_MATCH = -0x38C      # in a match

# id/password alphabet for issued accounts: the login screen promises IDs never use
# o/0, I/1 or 1/L, so leave those out.
_ISSUE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

# Lobby presence: uid (selected char id) -> the Session that entered the lobby (0x4202).
# Guarded by model.STATE.lock.
LOBBY = {}

# Longest a post-match user list waits for the opponent's stats (0x4508); the client
# allows ~15 s for the reply.
POST_MATCH_HOLD = 4.0

# A lobby session that has sent nothing for this long is treated as dead when the same
# character logs in again (the client heartbeats every ~3 s in the lobby/room/battle).
STALE_AFTER = 15.0

# The battle relay (relay.py), when POPN_P2P_RELAY=1; RELAY_HOST overrides the address
# handed out in 0x440b (default: the per-source advertise address, like 0x2002).
RELAY = None
RELAY_HOST = None


def u32(v):
    return wire.Writer().u32((v or 0) & 0xFFFFFFFF).bytes()


def parse_advertise_map(spec):
    """'cidr=ip,cidr=ip,...' -> [(network, ip)], first match wins. The 0x2002 server
    list must hand each client the next-hop IP reachable from ITS network (LAN, VPN
    or internet); the host field is an IP (char[15]), not a resolvable name."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        cidr, ip = part.split("=", 1)
        try:
            out.append((ipaddress.ip_network(cidr.strip(), strict=False), ip.strip()))
        except ValueError:
            continue
    return out


def advertise_for(peer_ip, amap, default):
    """The IP to advertise to a client at peer_ip (first matching CIDR, else default)."""
    try:
        addr = ipaddress.ip_address(peer_ip)
    except ValueError:
        return default
    for net, ip in amap:
        if addr in net:
            return ip
    return default


def parse_addr_map(spec):
    """'seen=give,seen=give' -> {seen: give} for POPN_P2P_ADDR_MAP."""
    out = {}
    for part in spec.split(","):
        if "=" in part:
            seen, give = part.split("=", 1)
            out[seen.strip()] = give.strip()
    return out


def svrlist(host, port, label="popn"):
    """The 0x2002 payload: one record per server type (1 = account, 0 = lobby), both
    pointing back here. Record (57 B): u32 type; char[32] label (EUC-JP); char[15] host;
    u32 port; u16 (unread). The client uses the first record of each type."""
    w = wire.Writer()
    for t in (1, 0):
        w.u32(t).fixed(label, 32).fixed(host, 15).u32(port).u16(port)
    return w.bytes()


def load_accounts():
    path = os.environ.get("POPN_AUTH_DB")
    if not path or not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def login_digest(login_id, challenge, password):
    """What the client puts in 0x4007: MD5(id || challenge || password)."""
    return hashlib.md5(login_id.encode("cp932") + challenge + password.encode("cp932")).digest()


def check_login(info, challenge, accounts, open_mode):
    """True if the digest matches a known password, or (unknown id) open mode is on."""
    pw = accounts.get(info["id"])
    if pw is not None and challenge is not None:
        return login_digest(info["id"], challenge, pw) == info["digest"]
    return open_mode


def issue_credential(n=8):
    return "".join(secrets.choice(_ISSUE_ALPHABET) for _ in range(n))


# The website's account API (gamelink.py). pop'n logins send MD5(id || challenge || password), so
# the server has to keep the password itself (Account.password, plaintext by necessity). An id that
# only ever logged in under POPN_AUTH_OPEN has none and accepts any password until one is set here.
def _stored_password(login_id):
    acct = model.STATE.accounts.get(login_id)
    if acct is None:
        return False, None
    return True, acct.password or load_accounts().get(login_id)


def link_verify(req):
    login_id = str(req.get("account", ""))
    with model.STATE.lock:
        known, pw = _stored_password(login_id)
    if not known:
        return {"ok": False, "error": "no_account"}
    if pw is None:
        return {"ok": False, "error": "no_password", "account": login_id, "key": login_id}
    if not hmac.compare_digest(pw.encode("cp932", "replace"),
                               str(req.get("password", "")).encode("cp932", "replace")):
        return {"ok": False, "error": "bad_password"}
    return {"ok": True, "account": login_id, "key": login_id}


def link_set_password(req):
    """4 to 12 letters and digits: the password box takes 12 (issued credentials are 8)."""
    pw = str(req.get("password", ""))
    if not (4 <= len(pw) <= 12 and pw.isascii() and pw.isalnum()):
        return {"ok": False, "error": "bad_new_password"}
    login_id = str(req.get("account", ""))
    with model.STATE.lock:
        acct = model.STATE.accounts.get(login_id)
        if acct is None:
            return {"ok": False, "error": "no_account"}
        acct.password = pw
        model.STATE.save()
    return {"ok": True, "account": login_id, "key": login_id}


def apply_match_result(members, winner):
    """Update both characters for a finished match (call with STATE.lock held).
    winner = the winner's uid, 0 = draw. Points use both ranks from BEFORE the match.
    Returns {uid: (outcome, points delta, new rank)}, or {} if a character is missing."""
    chars = {uid: model.STATE.character(uid) for uid in members}
    if len(chars) != 2 or any(c is None for c in chars.values()):
        return {}
    before = {uid: c.flags[model.RANK] for uid, c in chars.items()}
    out = {}
    for uid, c in chars.items():
        opp = next(u for u in chars if u != uid)
        outcome = "draw" if winner == 0 else ("win" if winner == uid else "loss")
        s = c.stats
        s[model.PLAYS] += 1
        s[{"win": model.WINS, "loss": model.LOSSES, "draw": model.DRAWS}[outcome]] += 1
        delta = model.points_delta(before[uid], before[opp], outcome)
        s[model.POINTS] = min(s[model.POINTS] + delta, 99_999_999)
        out[uid] = (outcome, delta, c.recompute_rank())
    model.STATE.save()
    live.record_match(out)
    return out


def release_held(room):
    """Send every held post-match user list (both stats in, or the hold timed out)."""
    with model.STATE.lock:
        held, room.held = room.held, []
        room.stats_pending.clear()
    for s in held:
        try:
            s.send_user_list()
        except OSError:
            pass


class Session:
    def __init__(self, sock, peer, log_dir, advertise="127.0.0.1", svr_port=5731, amap=(),
                 addr_map=None, idle=None):
        self.sock, self.peer = sock, peer
        self.advertise, self.svr_port, self.amap = advertise, svr_port, amap
        self.addr_map = addr_map or {}
        self.idle = idle          # recv timeout once in the lobby (heartbeats run there)
        self.wlock = threading.Lock()
        self.serial = 0
        self.challenge = None
        self.account_key = None   # plaintext login id
        self.uid = None           # selected character id (0x4106)
        self.room = None          # model.Room this session is in
        self.last_rx = time.monotonic()
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        self.log = open(os.path.join(log_dir, f"s-{stamp}-{peer[0]}-{peer[1]}.log"), "a", encoding="utf-8")
        self.events = open(os.path.join(log_dir, "events.jsonl"), "a", encoding="utf-8")

    # -- logging ----------------------------------------------------------------
    def event(self, kind, **kw):
        rec = {"t": datetime.datetime.now().isoformat(timespec="milliseconds"),
               "peer": f"{self.peer[0]}:{self.peer[1]}", "uid": self.uid, "kind": kind, **kw}
        try:
            self.events.write(json.dumps(rec) + "\n")
            self.events.flush()
        except (OSError, ValueError):
            pass

    def note(self, line):
        line = f"[{self.peer[0]}:{self.peer[1]} uid={self.uid}] {line}"
        try:
            self.log.write(line + "\n")
            self.log.flush()
        except (OSError, ValueError):
            pass
        try:
            print(line, flush=True)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"), flush=True)

    # -- sending ----------------------------------------------------------------
    def send(self, opcode, payload=b""):
        with self.wlock:
            self.serial += 1
            self.sock.sendall(wire.build(opcode, self.serial, payload))
            serial = self.serial
        self.note(f"-> {opcode:#06x} serial={serial} len={len(payload)} {payload.hex()}")
        self.event("send", op=f"{opcode:#06x}", serial=serial, payload=payload.hex())

    def push(self, opcode, payload=b""):
        """Send to this session from any thread; a dead peer is not our caller's problem."""
        try:
            self.send(opcode, payload)
        except OSError as e:
            self.note(f"   push {opcode:#06x} failed: {e!r}")

    @staticmethod
    def deliver(msgs):
        """Send (session, opcode, payload) triples in order. Built under the state lock,
        sent outside it."""
        for s, op, body in msgs:
            if s is not None:
                s.push(op, body)

    # -- identity helpers -------------------------------------------------------
    def ip(self):
        ip = self.peer[0]
        if ip.startswith("::ffff:"):
            ip = ip[7:]
        return ip

    def p2p_addr(self):
        """The IPv4 the OPPONENT must dial to reach this console, and the source address
        this console's listener will see: the address the gate sees, unless remapped."""
        ip = self.ip()
        return self.addr_map.get(ip, ip)

    def user_rec(self):
        return model.user_record(self.uid, model.STATE.character(self.uid),
                                 self.room.id if self.room else None)

    def everyone(self):
        return list(LOBBY.values())

    # -- presence -----------------------------------------------------------------
    def enter_lobby(self):
        if self.uid is None:
            return []
        msgs = []
        old = LOBBY.get(self.uid)
        if old is not None and old is not self:
            # The same character reconnected (stale socket): drop the old session.
            self.note(f"   replacing stale lobby session {old.peer}")
            msgs += old.leave_lobby_msgs(notify_self=False)
            old.close()
        LOBBY[self.uid] = self
        add = model.user_add_record(self.uid, model.STATE.character(self.uid))
        msgs += [(s, 0x4400, add) for s in self.everyone() if s is not self]
        return msgs

    def leave_lobby_msgs(self, notify_self):
        msgs = []
        if self.room is not None:
            msgs += self.leave_room_msgs(notify_self)
        if self.uid is not None and LOBBY.get(self.uid) is self:
            del LOBBY[self.uid]
            msgs += [(s, 0x4401, u32(self.uid)) for s in self.everyone()]
        return msgs

    def leave_room_msgs(self, notify_self):
        room, uid = self.room, self.uid
        self.room = None
        had_ready = bool(room.ready)
        if uid in room.members:
            room.members.remove(uid)
        room.ready.discard(uid)
        room.in_battle = False
        room.p2p_reporter = None
        msgs = []
        # The "left the room" line reads the leaver from the CURRENT room table, so it
        # must arrive before the update that removes them.
        for m in room.members:
            s = LOBBY.get(m)
            msgs.append((s, 0x4513, u32(0)))
            if had_ready:
                msgs.append((s, 0x4511, u32(0)))
        if room.members or room.seed:
            if room.owner not in room.members:
                room.owner = room.members[0] if room.members else 0
            body = room.upsert()
        else:
            model.STATE.remove_room(room.id)
            body = room.delete()
        targets = [s for s in self.everyone() if notify_self or s is not self]
        msgs += [(s, 0x4306, body) for s in targets]
        msgs += [(s, 0x4407, u32(uid)) for s in targets]
        self.note(f"   left room {room.id} -> {room!r}")
        return msgs

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    # -- dispatch -------------------------------------------------------------------
    def handle(self, f):
        self.last_rx = time.monotonic()
        self.note(f"<- {f!r} {f.payload.hex()}")
        self.event("recv", op=f"{f.opcode:#06x}", serial=f.serial, md5=f.digest_ok, payload=f.payload.hex())
        if f.opcode == GOODBYE:
            return False
        fn = HANDLERS.get(f.opcode)
        if fn is None:
            self.note(f"   unanswered {f.opcode:#06x}")
        else:
            fn(self, f.payload)
        return True

    # gate ---------------------------------------------------------------------------
    def on_hello(self, p):
        host = advertise_for(self.peer[0], self.amap, self.advertise)
        self.send(SVRLIST_REPLY, svrlist(host, self.svr_port))

    # account / login --------------------------------------------------------------
    def on_challenge(self, p):
        self.challenge = os.urandom(16)
        self.send(0x4004, self.challenge)

    def on_login(self, p):
        try:
            info = model.parse_login(p)
        except wire.FrameError as e:
            self.note(f"   login parse error: {e!r}")
            self.send(LOGIN_REPLY, u32(ERR_LOGIN))
            return
        login_id = info["id"]
        if model.STATE.adopt_legacy(model.legacy_account_key(p), login_id):
            self.note(f"   adopted the pre-cipher account for {login_id!r}")
        acct = model.STATE.account(login_id)
        pw = acct.password or load_accounts().get(login_id)
        if pw is not None and self.challenge is not None:
            ok = login_digest(login_id, self.challenge, pw) == info["digest"]
        else:
            ok = os.environ.get("POPN_AUTH_OPEN") == "1"
        if ok:
            self.account_key = login_id
        self.note(f"   login id={login_id!r} ok={ok} ({'password' if pw else 'open'})")
        self.event("login", id=login_id, ok=ok)
        self.send(LOGIN_REPLY, u32(0 if ok else ERR_LOGIN))

    def on_new_account(self, p):
        """0x4005: issue an id + password (12 chars max each). The id is shown to the
        player; the password only lives in their savedata."""
        with model.STATE.lock:
            while True:
                login_id = issue_credential()
                if login_id not in model.STATE.accounts:
                    break
            pw = issue_credential()
            model.STATE.issue_account(login_id, pw)
        self.note(f"   issued account {login_id!r}")
        self.event("issue", id=login_id)
        self.send(0x4006, bf.encrypt_payload(login_id.encode() + b"\0" + pw.encode() + b"\0"))

    def on_unlocks(self, p):
        self.send(0x451E, b"")                 # empty list = no presents

    def on_create_char(self, p):
        name, flags, extra = model.parse_create_char(p)
        acct = model.STATE.account(self.account_key or "default")
        disp = name or f"POPN{len(acct.characters) + 1}"
        c = model.STATE.add_character(acct.key, disp, flags=flags, extra=extra)
        self.note(f"   created char id={c.id} name={disp!r} flags={flags} extra={extra}")
        self.send(0x4101, u32(0))

    def on_delete_char(self, p):
        cid = wire.Reader(p).u32() if len(p) >= 4 else 0
        ok = model.STATE.delete_character(self.account_key or "default", cid)
        self.note(f"   delete char id={cid} ok={ok}")
        self.send(0x4103, u32(0))

    def on_char_list(self, p):
        acct = model.STATE.account(self.account_key or "default")
        if not acct.characters and os.environ.get("POPN_FAKE_CHAR") == "1":
            model.STATE.add_character(acct.key, os.environ.get("POPN_CHAR_NAME", "POPN1"))
        body = acct.char_list_payload()
        self.send(0x4105, body)
        self.note(f"   char list: {[(c.id, c.name) for c in acct.characters]}")

    # lobby ----------------------------------------------------------------------------
    def on_select_char(self, p):
        self.uid = wire.Reader(p).u32() if len(p) >= 4 else None
        self.note(f"   selected char id={self.uid}")
        self.send(0x4107, u32(0))

    def on_enter_lobby(self, p):
        with model.STATE.lock:
            old = LOBBY.get(self.uid) if self.uid is not None else None
            live = (old is not None and old is not self
                    and time.monotonic() - old.last_rx < STALE_AFTER)
        if live:
            # The same character is already online on another console: refuse with the
            # client's own "already logged in" message rather than kicking that player.
            self.note(f"   char {self.uid} already in the lobby from {old.peer}; refusing")
            self.send(0x4203, u32(ERR_LOGGED_IN))
            return
        with model.STATE.lock:
            msgs = self.enter_lobby()          # register first, then answer
        self.send(0x4203, u32(0))
        if self.idle:
            # The lobby, room and battle scenes heartbeat (0x4514) every ~3 s, so long
            # silence here means the console is gone; drop it so its presence clears.
            # (Not earlier: the character-creation screen does not heartbeat.)
            self.sock.settimeout(self.idle)
        self.deliver(msgs)

    def on_leave_lobby(self, p):
        self.send(0x4205, u32(0))
        with model.STATE.lock:
            msgs = self.leave_lobby_msgs(notify_self=False)
        self.deliver(msgs)

    def on_user_list(self, p):
        """0x4402. Right after a match the result screen reads both players' new points
        and rank from this reply exactly once, so hold it until the opponent's stats
        (0x4508) are in too, or POST_MATCH_HOLD passes."""
        with model.STATE.lock:
            room = self.room
            if room is not None and room.stats_pending:
                room.held.append(self)
                self.note(f"   user list held until stats from {sorted(room.stats_pending)}")
                return
        self.send_user_list()

    def send_user_list(self):
        """0x4403{n} + <=10 records per 0x4404 + 0x4405 (event 5 only fires if n ==
        records). Every online user, always including this one: the client takes its
        current room from its own record."""
        with model.STATE.lock:
            sessions = self.everyone()
            if self.uid is not None and self not in sessions:
                sessions.append(self)
            recs = [s.user_rec() for s in sessions if s.uid is not None]
        self.send(0x4403, u32(len(recs)))
        for i in range(0, max(len(recs), 1), 10):
            self.send(0x4404, b"".join(recs[i:i + 10]))
        self.send(0x4405, b"")
        self.note(f"   user list: {len(recs)} user(s)")

    def on_room_list(self, p):
        """0x4302 -> 0x4303 (resets the whole table) + <=10 records per 0x4304 + 0x4305."""
        with model.STATE.lock:
            recs = [r.record() for r in model.STATE.room_list()]
        self.send(0x4303, u32(len(recs)))
        for i in range(0, max(len(recs), 1), 10):
            self.send(0x4304, b"".join(recs[i:i + 10]))
        self.send(0x4305, u32(0))
        self.note(f"   room list: {len(recs)} room(s)")

    def on_chat(self, p):
        """0x4408 from the client: u8 junk; u32 -1; text (no NUL). Broadcast to the same
        context (lobby, or the room) AND echo to the sender, who only sees their own line
        through the echo."""
        text = p[5:].split(b"\0", 1)[0].split(b"\n", 1)[0][:130]
        body = wire.Writer().u8(1).u32(self.uid or 0).bytes() + text + b"\0"
        with model.STATE.lock:
            if self.room is not None:
                targets = [LOBBY.get(m) for m in self.room.members]
            else:
                targets = [s for s in self.everyone() if s.room is None]
            if self not in targets:
                targets.append(self)
        self.deliver([(s, 0x4408, body) for s in targets])

    # rooms --------------------------------------------------------------------------------
    def on_create_room(self, p):
        name, password, a, b = model.parse_create_room(p)
        with model.STATE.lock:
            msgs = self.leave_room_msgs(notify_self=True) if self.room else []
            room = model.STATE.create_room(name, password, a, b, owner=self.uid or 0)
            self.room = room
            msgs += [(self, 0x4301, u32(room.id)), (self, 0x430A, u32(room.id))]
            for s in self.everyone():
                msgs += [(s, 0x4406, u32(self.uid) + u32(room.id)), (s, 0x4306, room.upsert())]
        self.note(f"   created {room!r}")
        self.deliver(msgs)

    def on_join_room(self, p):
        r = wire.Reader(p)
        rid = r.u32() if len(p) >= 4 else 0
        given = p[5:].split(b"\0", 1)[0] if len(p) > 5 else b""
        with model.STATE.lock:
            room = model.STATE.rooms.get(rid)
            if room is None:
                status = ERR_NO_ROOM
            elif self.uid in room.members:
                status = rid
            elif len(room.members) >= model.ROOM_CAPACITY:
                status = ERR_ROOM_FULL
            elif room.in_battle:
                status = ERR_IN_MATCH
            elif room.password and given != room.password:
                status = ERR_ROOM_PW
            else:
                status = rid
            if status != rid:
                msgs = [(self, 0x430A, u32(status))]
            else:
                if self.room is not None and self.room is not room:
                    pre = self.leave_room_msgs(notify_self=True)
                else:
                    pre = []
                if self.uid not in room.members:
                    room.members.append(self.uid)
                if room.owner not in room.members:
                    room.owner = self.uid
                self.room = room
                msgs = pre + [(self, 0x430A, u32(rid))]
                for s in self.everyone():
                    msgs += [(s, 0x4406, u32(self.uid) + u32(rid)), (s, 0x4306, room.upsert())]
                for m in room.members:
                    if m != self.uid:
                        msgs.append((LOBBY.get(m), 0x4512, u32(0)))
        self.note(f"   join room {rid}: status={status} given_pw={given!r}")
        self.deliver(msgs)

    def on_leave_room(self, p):
        self.send(0x430C, u32(0))
        with model.STATE.lock:
            msgs = self.leave_room_msgs(notify_self=True) if self.room else []
        self.deliver(msgs)

    def on_room_settings(self, p):
        flags, pw, a, b = model.parse_room_settings(p)
        with model.STATE.lock:
            room = self.room
            if room is None or room.owner != self.uid:
                msgs = [(self, 0x450C, u32(ERR_NOT_OWNER))]
            elif room.in_battle:
                msgs = [(self, 0x450C, u32(ERR_IN_MATCH))]
            else:
                if flags & 2:
                    room.a = a
                if flags & 1:
                    room.b = b
                if flags & 4:
                    room.password = pw or None
                if flags & 8:
                    room.password = None
                msgs = [(self, 0x450C, u32(0))]
                msgs += [(s, 0x4306, room.upsert()) for s in self.everyone()]
        self.note(f"   settings flags={flags:#x} A={a} B={b} -> {self.room!r}")
        self.deliver(msgs)

    # match ------------------------------------------------------------------------------
    def _peer(self):
        room = self.room
        if room is None:
            return None, None
        peer = room.peer_of(self.uid)
        return peer, LOBBY.get(peer) if peer is not None else None

    def on_ready(self, p):
        """0x4500 START. First press: 0x4503 to the presser (clears its 15 s timer, keeps
        it waiting) + 0x4510 to the peer (ready marker, START locked). Second press:
        0x4506 ("go") to the presser + 0x4510 to the first player, who then starts too."""
        with model.STATE.lock:
            room = self.room
            peer, ps = self._peer()
            if room is None:
                msgs = [(self, 0x4503, u32(0))]
            else:
                room.ready.add(self.uid)
                room.p2p_reporter = None
                if peer is not None and peer in room.ready:
                    msgs = [(self, 0x4506, u32(0)), (ps, 0x4510, u32(0))]
                else:
                    msgs = [(self, 0x4503, u32(0)), (ps, 0x4510, u32(0))]
            ready = sorted(room.ready) if room else []
        self.note(f"   ready: room ready={ready}")
        self.deliver(msgs)

    def on_cancel(self, p):
        """0x4501 cancel / abort: ack the sender, unlock the peer."""
        with model.STATE.lock:
            room = self.room
            peer, ps = self._peer()
            if room is not None:
                room.ready.discard(self.uid)
                room.in_battle = False
                room.p2p_reporter = None
            msgs = [(self, 0x4503, u32(0)), (ps, 0x4511, u32(0))]
        self.deliver(msgs)

    def on_peer_addr(self, p):
        """0x4409{opp uid} -> 0x440b{opp uid; "a.b.c.d\\0"}: where to find the opponent.
        With the relay on (relay-patched clients connect out instead of listening), the
        address is the relay's, reached over the same path as this gate, and this
        player is registered for the match so the relay can pair the two connections."""
        opp = wire.Reader(p).u32() if len(p) >= 4 else 0
        if RELAY is not None:
            with model.STATE.lock:
                key = ("room", self.room.id) if self.room else ("pair", tuple(sorted((self.uid or 0, opp))))
            RELAY.expect(key, self.ip())
            addr = RELAY_HOST or advertise_for(self.peer[0], self.amap, self.advertise)
            self.note(f"   peer address for {opp}: relay {addr} (match {key})")
            self.send(0x440B, u32(opp) + addr.encode("ascii")[:15] + b"\0")
            return
        with model.STATE.lock:
            s = LOBBY.get(opp)
            addr = s.p2p_addr() if s is not None else "0.0.0.0"
        if s is not None and s.ip() == self.ip():
            self.note(f"   WARNING: opponent {opp} has the same source IP {addr}; on one LAN "
                      f"behind NAT the P2P listener will reject the connection (set POPN_P2P_ADDR_MAP)")
        self.note(f"   peer address for {opp}: {addr}")
        self.send(0x440B, u32(opp) + addr.encode("ascii")[:15] + b"\0")

    def on_p2p_failed(self, p):
        """0x451f: the P2P setup failed on this side. The first report of a round is
        echoed to the reporter (retry now) and forwarded to the peer (abort its attempt,
        retry with roles swapped). A second report in the same round is the peer reacting
        to that abort; it is already retrying, and another 0x451f would abort the retry,
        so it gets nothing (its timer is cleared later by 0x4518 or 0x451a)."""
        with model.STATE.lock:
            room = self.room
            peer, ps = self._peer()
            if room is not None and room.p2p_reporter is None:
                room.p2p_reporter = self.uid
                msgs = [(self, 0x451F, u32(0)), (ps, 0x451F, u32(0))]
            elif room is None:
                msgs = [(self, 0x451F, u32(0))]
            else:
                msgs = []
        self.note(f"   P2P setup failed; {'forwarded' if msgs else 'second report, absorbed'}")
        self.deliver(msgs)

    def on_battle_start(self, p):
        """0x4517 (owner, P2P is up) -> 0x4518 to both; the guest gets it too so any timer
        still armed from a P2P retry is cleared."""
        with model.STATE.lock:
            room = self.room
            peer, ps = self._peer()
            if room is not None:
                room.in_battle = True
                room.ready.clear()
                room.p2p_reporter = None
            msgs = [(self, 0x4518, u32(0)), (ps, 0x4518, u32(0))]
        self.note("   battle started")
        self.deliver(msgs)

    def on_result(self, p):
        """0x4519{u8 code; u32 uid}: 0x20 = finished (uid = winner, 0 = draw), 0x21 =
        aborted. A finished result goes to BOTH players (the guest waits for it), and is
        when both characters' plays / wins / losses / draws / points / rank change; an
        abort changes nothing."""
        code = p[0] if p else 0
        winner = wire.Reader(p[1:]).u32() if len(p) >= 5 else 0
        changes = {}
        with model.STATE.lock:
            room = self.room
            peer, ps = self._peer()
            if room is not None:
                room.in_battle = False
                room.ready.clear()
                room.p2p_reporter = None
                if code == 0x20 and len(room.members) == 2:
                    changes = apply_match_result(room.members, winner)
                    if changes:
                        room.stats_pending = set(room.members)
                        room.held = []
                        threading.Timer(POST_MATCH_HOLD, release_held, args=(room,)).start()
            msgs = [(self, 0x451A, u32(0))]
            if code == 0x20:
                msgs.append((ps, 0x451A, u32(0)))
        self.note(f"   result code={code:#x} winner={winner} room={room!r} stats={changes}")
        self.event("result", code=code, winner=winner, peer=peer, stats=changes)
        self.deliver(msgs)

    def on_stats(self, p):
        """0x4508 (124 B per-player battle stats) -> 0x450a. The u32 at +4 is this game's
        score: it feeds the high score, which feeds the rank. Once both players of a
        finished match have reported, release their held user lists and tell everyone
        else in the lobby (0x450f)."""
        score = wire.Reader(p[4:8]).u32() if len(p) >= 8 else 0
        self.event("stats", payload=p.hex(), score=score)
        msgs, release = [], None
        with model.STATE.lock:
            c = model.STATE.character(self.uid)
            if c is not None and score:
                c.stats[model.HISCORE] = max(c.stats[model.HISCORE], min(score, 99_999_999))
                c.recompute_rank()
                model.STATE.save()
            room = self.room
            if room is not None and self.uid in room.stats_pending:
                battle = model.parse_battle(p)
                if c is not None and battle is not None:
                    model.STATE.record_battle(c, battle)
                room.stats_pending.discard(self.uid)
                if not room.stats_pending:
                    release = room
                    others = [s for s in self.everyone() if s.uid not in room.members]
                    for uid in room.members:
                        rec = model.stats_record(uid, model.STATE.character(uid))
                        msgs += [(s, 0x450F, rec) for s in others]
        self.send(0x450A, u32(0))
        if release is not None:
            release_held(release)
        self.deliver(msgs)

    def on_beginner(self, p):
        self.send(0x4523, u32(0))

    def on_heartbeat(self, p):
        pass                                   # 0x4514 needs no reply

    # -- connection loop --------------------------------------------------------------
    def run(self):
        dec = wire.Decoder()
        self.event("open")
        try:
            while True:
                data = self.sock.recv(4096)
                if not data:
                    break
                for f in dec.feed(data):
                    if not self.handle(f):
                        return
        except (OSError, wire.FrameError) as e:
            self.note(f"closed: {e!r}")
        finally:
            try:
                with model.STATE.lock:
                    msgs = self.leave_lobby_msgs(notify_self=False)
                self.deliver(msgs)
            except Exception as e:     # never let cleanup kill the thread silently
                self.note(f"cleanup error: {e!r}")
            self.event("close")
            try:
                self.sock.close()
            except OSError:
                pass
            self.log.close()
            self.events.close()


HANDLERS = {
    HELLO: Session.on_hello,
    CHALLENGE: Session.on_challenge,
    LOGIN: Session.on_login,
    0x4005: Session.on_new_account,
    0x451D: Session.on_unlocks,
    0x4100: Session.on_create_char,
    0x4102: Session.on_delete_char,
    0x4104: Session.on_char_list,
    0x4106: Session.on_select_char,
    0x4202: Session.on_enter_lobby,
    0x4204: Session.on_leave_lobby,
    0x4402: Session.on_user_list,
    0x4302: Session.on_room_list,
    0x4408: Session.on_chat,
    0x4300: Session.on_create_room,
    0x4309: Session.on_join_room,
    0x430B: Session.on_leave_room,
    0x450B: Session.on_room_settings,
    0x4500: Session.on_ready,
    0x4501: Session.on_cancel,
    0x4409: Session.on_peer_addr,
    0x451F: Session.on_p2p_failed,
    0x4517: Session.on_battle_start,
    0x4519: Session.on_result,
    0x4508: Session.on_stats,
    0x4522: Session.on_beginner,
    0x4514: Session.on_heartbeat,
}


def main():
    bind = os.environ.get("POPN_BIND", "0.0.0.0")
    port = int(os.environ.get("POPN_PORT", "5731"))
    log_dir = os.environ.get("POPN_LOG_DIR", os.path.join("data", "logs"))
    os.makedirs(log_dir, exist_ok=True)
    advertise = os.environ.get("POPN_ADVERTISE") or bind
    svr_port = int(os.environ.get("POPN_SVR_PORT", str(port)))
    amap = parse_advertise_map(os.environ.get("POPN_ADVERTISE_MAP", ""))
    addr_map = parse_addr_map(os.environ.get("POPN_P2P_ADDR_MAP", ""))
    idle = float(os.environ.get("POPN_IDLE_TIMEOUT", "120"))
    state_db = os.environ.get("POPN_STATE_DB", os.path.join("data", "accounts.json"))
    model.init(state_db)
    live.start(LOBBY, state_db)
    global RELAY, RELAY_HOST
    if os.environ.get("POPN_P2P_RELAY") == "1":
        RELAY = relay.Relay()
        RELAY_HOST = os.environ.get("POPN_RELAY_HOST") or None
        rport = int(os.environ.get("POPN_RELAY_PORT", "5730"))
        threading.Thread(target=RELAY.serve, args=(bind, rport), daemon=True).start()
    if os.environ.get("POPN_SEED_ROOM") == "1":
        model.STATE.create_room(os.environ.get("POPN_SEED_NAME", "TEST").encode("cp932"),
                                None, 0, 2, owner=0, seed=True)
    gamelink.serve({"/v1/verify": link_verify, "/v1/set-password": link_set_password},
                   int(os.environ.get("POPN_LINK_PORT", "5739")), log=lambda m: print(m, flush=True))
    adminport.start(LOBBY, log=lambda m: print(m, flush=True))   # an OpenLobby admin panel
    srv = socket.create_server((bind, port), reuse_port=False)
    print(f"popn gate on {bind}:{port}, logs in {log_dir}; server list -> {advertise}:{svr_port}; "
          f"state -> {state_db} ({len(model.STATE.accounts)} account(s))", flush=True)
    while True:
        sock, peer = srv.accept()
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=Session(sock, peer, log_dir, advertise, svr_port, amap,
                                        addr_map, idle).run, daemon=True).start()


if __name__ == "__main__":
    main()
