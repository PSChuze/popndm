"""Server-side state for the pop'n lobby: accounts, characters, rooms.

The gate runs one thread per connection, so all shared state lives in a single `STATE`
guarded by a re-entrant lock. Accounts + characters persist to a JSON file (they
survive restarts); rooms are ephemeral (they only exist while their players are
connected, so they are never persisted).

Identity: the client's uid everywhere in the lobby, room and match messages is its
SELECTED CHARACTER ID (0x4106). Character ids are therefore allocated from one global
counter, never per account. Accounts are keyed on the plaintext login id (0x4007
deciphered with bf.py).

Wire record formats:
    0x4105 char       u32 id; str name; u32 s1..s6; u8 f1..f3
    0x4404 user (71B) name[32]; uid; s2; s3; s4; s1; status; s6; s5; location; f1; f2; f3
    0x4400 add  (63B) name[32]; uid; s2; s3; s4; s5; status; s6; f1; f2; f3
    0x450f stats(31B) uid; s1..s6; f1..f3
    0x4304 room       id; sortkey; name\\0; M; M x uid; owner; state; A; B; has_pw
    0x4306 upsert     id; sortkey; state; [name\\0; M; M x uid; owner; has_pw; A; B]
s1..s6 / f1..f3 are the character's 0x4105 values in 0x4105 order: s1 plays, s2 wins,
s3 losses, s4 draws, s5 points, s6 high score; f1 rank (0..17), f2 prefecture (0..46),
f3 player class. Rank follows the client's own formula (rank()).
"""
import json
import os
import re
import threading

from . import bf
from . import wire

MAX_CHARS = 5            # the 0x4105 parser inits 5 record slots; more corrupts ctx
STATUS_LOBBY = 0x3F1     # user record state: in the lobby
STATUS_ROOM = 0x3F2      # user record state: in a room
NO_ROOM = 0xFFFFFFFF     # user record location when not in a room
ROOM_DELETE = 0x3EB      # 0x4306 state value that deletes the room
ROOM_STATE = 0           # 0x4304/0x4306 state for a live room (other values unknown)
ROOM_CAPACITY = 2

_PLACEHOLDER = re.compile(r"^POPN\d+$")

# Character stats, in 0x4105 wire order:
#   s1 plays, s2 wins, s3 losses, s4 draws (never shown), s5 points (Pt), s6 high score
#   f1 rank 0..17 (8th kyu..god), f2 prefecture 0..46, f3 player class (client default
#   1)
PLAYS, WINS, LOSSES, DRAWS, POINTS, HISCORE = range(6)
RANK, PREF, CLASS = range(3)
FLAGS_VERSION = 2        # char flags written as (rank, pref, class); older files used (B, D, E)

# The client's own rank formula, rank(plays, wins, hiscore) (disc client 0x20c180).
# The character list recomputes the badge with it, so f1 must equal it.
_PLAY_BRACKETS = ((10, 0, -100), (20, 2, -80), (30, 4, -60), (40, 6, -40), (50, 8, -20),
                  (100000, 10, 0))
_HISCORE_BONUS = ((20000, 0), (30000, 5), (40000, 10), (50000, 15), (60000, 20),
                  (70000, 30), (80000, 40), (100000000, 50))
_RANK_THRESHOLDS = (-99, -75, -50, -25, 0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 150, 200)


def rank(plays, wins, hiscore):
    """Rank 0..17 exactly as the client computes it (MIPS division truncates toward 0)."""
    v = wins - (plays - wins)
    for lim, add in _HISCORE_BONUS:
        if hiscore < lim:
            v += add
            break
    for lim, mul, add in _PLAY_BRACKETS:
        if plays < lim:
            v = int(v * mul / 10) + add
            break
    for i, t in enumerate(_RANK_THRESHOLDS):
        if v < t:
            return i
    return 17


# 0x4508 per-player battle stats, 124 B big-endian:
#   +0 u8 outcome 4 win / 8 lose / 0x10 draw; +1 u8 character*2 + variant (the id of the
#   chara/attack ranking tables, < 0x32); +2 u8 battle setting; +3 u8 low byte of the game's
#   running total that each move's attack count is added to (attack balls sent, wraps at
#   256); +4 u32 score; +8 u32 balls placed by the per-character attack routines (sent or
#   received: unresolved); +12 u32 balls cleared this game (running sum); +16 u32 max
#   chain; +20 u32[26] chain-length histogram.
BATTLE_LEN = 124
BATTLE_KEYS = ("games", "max_chain", "balls_total", "balls_best", "atk_total", "drop_total")


def parse_battle(p):
    """The 0x4508 fields as a dict, or None if the record is short."""
    if len(p) < BATTLE_LEN:
        return None
    r = wire.Reader(p)
    outcome, chara, setting, atk = r.u8(), r.u8(), r.u8(), r.u8()
    score, drops, balls, max_chain = r.u32(), r.u32(), r.u32(), r.u32()
    hist = [r.u32() for _ in range(26)]
    return {"outcome": outcome, "chara": chara, "setting": setting, "atk": atk,
            "score": score, "drops": drops, "balls": balls, "max_chain": max_chain,
            "hist": hist}


def points_delta(me_rank, opp_rank, outcome):
    """Points for one match. The official rule is not in the client, so this is our own:
    win 30 (+3 per rank the opponent is above you), draw 15, loss 10. Never negative:
    the result screen clamps the shown gain at 0."""
    if outcome == "win":
        return 30 + 3 * max(0, opp_rank - me_rank)
    if outcome == "draw":
        return 15
    return 10


def printable_cp932(raw):
    """A display string if `raw` is clean cp932 text, else None."""
    s = raw.split(b"\x00", 1)[0]
    if not s:
        return None
    try:
        text = s.decode("cp932")
    except UnicodeDecodeError:
        return None
    if any(ord(c) < 0x20 for c in text):
        return None
    return text


def name_bytes(name, n=0x20):
    """A name as the client stores it: cp932, cut so a NUL always fits in `n` bytes."""
    b = name.encode("cp932", "replace") if isinstance(name, str) else bytes(name)
    return b.split(b"\0", 1)[0][:n - 1]


class Character:
    def __init__(self, cid, name, raw_name=b"", stats=None, flags=(0, 0, 1), extra=None,
                 fv=FLAGS_VERSION, battle=None):
        self.id = cid
        self.name = name              # str (cp932-safe), served as-is in 0x4105/0x4404
        self.raw_name = raw_name      # legacy: enciphered 0x4100 name bytes (pre-cipher)
        self.stats = list(stats) if stats else [0, 0, 0, 0, 0, 0]   # s1..s6, see PLAYS..
        self.flags = list(flags)      # f1 rank, f2 prefecture, f3 class
        self.extra = dict(extra or {})   # 0x4100 profile: postal A, birthday C, job D, sex E
        self.battle = {k: 0 for k in BATTLE_KEYS}   # lifetime 0x4508 totals (rankings)
        self.battle.update(battle or {})
        self.fv = fv                  # flags layout version (FLAGS_VERSION)

    def recompute_rank(self):
        s = self.stats
        self.flags[RANK] = rank(s[PLAYS], s[WINS], s[HISCORE])
        return self.flags[RANK]

    def record(self):
        """One 0x4105 wire record: u32 id; str name(NUL); u32 x6; u8 x3."""
        w = wire.Writer().u32(self.id)
        w.b += name_bytes(self.name, 0x24) + b"\0"
        for s in self.stats:
            w.u32(s)
        for fb in self.flags:
            w.u8(fb)
        return w.bytes()

    def to_json(self):
        return {"id": self.id, "name": self.name, "raw_name": self.raw_name.hex(),
                "stats": self.stats, "flags": self.flags, "extra": self.extra, "fv": self.fv,
                "battle": self.battle}

    @classmethod
    def from_json(cls, d):
        return cls(d["id"], d["name"], bytes.fromhex(d.get("raw_name", "")),
                   d.get("stats"), d.get("flags") or (0, 0, 1), d.get("extra"),
                   d.get("fv", 1), d.get("battle"))


class Account:
    def __init__(self, key, password=None):
        self.key = key                # plaintext login id (or a legacy hex key)
        self.password = password      # plaintext password if known (issued via 0x4005)
        self.characters = []          # list[Character], newest last, capped at MAX_CHARS

    def char_list_payload(self):
        """The 0x4105 body = every character's record concatenated (<= 5)."""
        return b"".join(c.record() for c in self.characters[-MAX_CHARS:])

    def to_json(self):
        return {"key": self.key, "password": self.password,
                "characters": [c.to_json() for c in self.characters]}

    @classmethod
    def from_json(cls, d):
        a = cls(d["key"], d.get("password"))
        a.characters = [Character.from_json(c) for c in d.get("characters", [])]
        return a


class Room:
    def __init__(self, rid, name, password, a, b, owner, seed=False):
        self.id = rid
        self.name = name              # bytes (plaintext Shift-JIS)
        self.password = password      # bytes or None
        self.a = a                    # setting A (0..2)
        self.b = b                    # setting B (0..4, handicap)
        self.owner = owner            # uid (char id) of the host; 0 = none
        self.members = []             # uids currently in the room (max 2)
        self.seed = seed              # a seeded room survives being empty
        # match state
        self.ready = set()            # uids that sent 0x4500
        self.in_battle = False
        self.p2p_reporter = None      # uid that reported the current P2P failure round
        # post-match: uids whose 0x4508 is still due; 0x4402 replies held until they land
        self.stats_pending = set()
        self.held = []                # sessions waiting for their post-match user list

    def __repr__(self):
        pw = "" if self.password is None else " pw"
        return (f"<Room {self.id} {self.name!r}{pw} A={self.a} B={self.b} "
                f"owner={self.owner} members={self.members}>")

    def peer_of(self, uid):
        for m in self.members:
            if m != uid:
                return m
        return None

    def record(self):
        """0x4304 room-list record."""
        w = wire.Writer().u32(self.id).u32(self.id)
        w.b += name_bytes(self.name, 0x28) + b"\0"
        w.u32(len(self.members))
        for m in self.members:
            w.u32(m)
        w.u32(self.owner).u32(ROOM_STATE)
        w.u8(self.a).u8(self.b).u8(1 if self.password else 0)
        return w.bytes()

    def upsert(self):
        """0x4306 push: insert/update this room (note the u8 order differs from 0x4304)."""
        w = wire.Writer().u32(self.id).u32(self.id).u32(ROOM_STATE)
        w.b += name_bytes(self.name, 0x28) + b"\0"
        w.u32(len(self.members))
        for m in self.members:
            w.u32(m)
        w.u32(self.owner)
        w.u8(1 if self.password else 0).u8(self.a).u8(self.b)
        return w.bytes()

    def delete(self):
        """0x4306 push: remove this room."""
        return wire.Writer().u32(self.id).u32(self.id).u32(ROOM_DELETE).bytes()


class ServerState:
    def __init__(self, path=None):
        self.lock = threading.RLock()
        self.path = path
        self.accounts = {}            # key -> Account
        self.rooms = {}               # id -> Room (ephemeral)
        self._next_char = 1           # GLOBAL: char id = the client's uid
        self._next_room = 1
        self.chara = {}               # str(0x4508 +1 id) -> {games, wins, atk_total, ...}
        self.load()

    # -- accounts / characters (persisted) --
    def account(self, key):
        with self.lock:
            a = self.accounts.get(key)
            if a is None:
                a = self.accounts[key] = Account(key)
                self.save()
            return a

    def adopt_legacy(self, legacy_key, key):
        """Move an account stored under its pre-cipher key (hex of the enciphered id)
        to its plaintext id, keeping its characters. No-op if there is nothing to move."""
        with self.lock:
            if not legacy_key or key in self.accounts or legacy_key not in self.accounts:
                return False
            a = self.accounts.pop(legacy_key)
            a.key = key
            self.accounts[key] = a
            self.save()
            return True

    def issue_account(self, login_id, password):
        with self.lock:
            a = self.accounts[login_id] = Account(login_id, password)
            self.save()
            return a

    def add_character(self, key, name, raw_name=b"", stats=None, flags=(0, 0, 1), extra=None):
        with self.lock:
            acct = self.account(key)
            c = Character(self._next_char, name, raw_name, stats, flags, extra)
            self._next_char += 1
            acct.characters.append(c)
            del acct.characters[:-MAX_CHARS]
            self.save()
            return c

    def delete_character(self, key, cid):
        with self.lock:
            acct = self.account(key)
            before = len(acct.characters)
            acct.characters = [c for c in acct.characters if c.id != cid]
            if len(acct.characters) != before:
                self.save()
                return True
            return False

    def record_battle(self, char, b):
        """Fold one parsed 0x4508 (parse_battle) into the character's lifetime totals and
        the per-character-id table. Call once per player per finished match."""
        with self.lock:
            t = char.battle
            t["games"] += 1
            t["max_chain"] = max(t["max_chain"], b["max_chain"])
            t["balls_total"] += b["balls"]
            t["balls_best"] = max(t["balls_best"], b["balls"])
            t["atk_total"] += b["atk"]
            t["drop_total"] += b["drops"]
            ch = self.chara.setdefault(str(b["chara"]), {"games": 0, "wins": 0,
                                                         "atk_total": 0, "drop_total": 0})
            ch["games"] += 1
            ch["wins"] += b["outcome"] == 4
            ch["atk_total"] += b["atk"]
            ch["drop_total"] += b["drops"]
            self.save()

    def character(self, cid):
        with self.lock:
            for a in self.accounts.values():
                for c in a.characters:
                    if c.id == cid:
                        return c
            return None

    # -- rooms (ephemeral) --
    def create_room(self, name, password, a, b, owner, seed=False):
        with self.lock:
            rid = self._next_room
            self._next_room += 1
            r = self.rooms[rid] = Room(rid, name, password, a, b, owner, seed)
            if owner:
                r.members.append(owner)
            return r

    def room_list(self):
        with self.lock:
            return sorted(self.rooms.values(), key=lambda r: r.id)

    def remove_room(self, rid):
        with self.lock:
            return self.rooms.pop(rid, None)

    # -- persistence --
    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            return
        with self.lock:
            self.accounts = {k: Account.from_json(v) for k, v in d.get("accounts", {}).items()}
            self._next_char = d.get("next_char", 1)
            self.chara = d.get("chara", {})
            if self._migrate():
                self.save()

    def _migrate(self):
        """Bring an older accounts.json up to date. Character ids used to start at 1 per
        account (so two accounts collided on uid 1); make them globally unique. Names
        stored as POPN<n> placeholders were enciphered; decrypt them now that the
        cipher is known. Returns True if anything changed."""
        changed = False
        used = set()
        chars = [c for a in self.accounts.values() for c in a.characters]
        top = max([c.id for c in chars] + [self._next_char - 1, 0])
        for c in chars:
            if c.id <= 0 or c.id in used:
                top += 1
                c.id = top
                changed = True
            used.add(c.id)
            if c.fv < FLAGS_VERSION:
                # Older files stored the 0x4100 bytes (B pref, D job, E sex) as f1..f3; the
                # client reads f1 as RANK and f2 as PREFECTURE. Move B to f2, recompute rank.
                old = (list(c.flags) + [0, 0, 1])[:3]
                c.flags = [0, old[0] if 0 <= old[0] <= 46 else 0, 1]
                c.recompute_rank()
                c.fv = FLAGS_VERSION
                changed = True
            if _PLACEHOLDER.match(c.name or "") and len(c.raw_name) >= 8:
                plain = bf.decrypt_payload(c.raw_name[:len(c.raw_name) // 8 * 8])
                real = printable_cp932(plain[:0x20])
                if real:
                    c.name = real
                    changed = True
        if self._next_char <= top:
            self._next_char = top + 1
            changed = True
        return changed

    def save(self):
        if not self.path:
            return
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"accounts": {k: a.to_json() for k, a in self.accounts.items()},
                           "next_char": self._next_char, "chara": self.chara},
                          fh, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass


# -- payload parsers --------------------------------------------------------

def legacy_account_key(login_payload):
    """The key older gates stored accounts under: hex of the ENCIPHERED id field.
    Deterministic per login id (ECB, and the digest sits past this window), so it lets
    a returning player's characters be adopted under their plaintext id."""
    idfield = login_payload[1:1 + 0x100]
    return idfield.rstrip(b"\x00")[:16].hex() or None


def parse_login(payload):
    """0x4007 (deciphered): u8 flag; char[256] id; md5[16]."""
    plain = bf.decrypt_payload(payload)
    r = wire.Reader(plain)
    flag = r.u8()
    login_id = r.fixed(0x100).split(b"\0", 1)[0].decode("cp932", "replace")
    digest = r.fixed(16) if len(plain) >= 1 + 0x100 + 16 else b""
    return {"flag": flag, "id": login_id, "digest": digest}


def parse_create_char(payload):
    """0x4100 (deciphered): char[32] name (SJIS); u32 A postal code; u32 C birthday
    (CYYMMDD); u8 B prefecture 0..46; u8 D occupation; u8 E sex.
    Returns (name or None, flags (rank 0, prefecture B, class 1), extra profile)."""
    plain = bf.decrypt_payload(payload)
    name = printable_cp932(plain[:0x20])
    r = wire.Reader(plain[0x20:])
    try:
        a, c = r.u32(), r.u32()
        b, d, e = r.u8(), r.u8(), r.u8()
    except wire.FrameError:
        return name, (0, 0, 1), {}
    return name, (0, b if b <= 46 else 0, 1), {"A": a, "C": c, "D": d, "E": e}


def parse_create_room(payload):
    """0x4300: u8 has_pw; char[32] name; {char[32] pw if has_pw}; u8 A; u8 B.
    Returns (name:bytes, password:bytes or None, A, B)."""
    has_pw = payload[0] if payload else 0
    name = payload[1:0x21].split(b"\x00", 1)[0]
    rest = payload[0x21:]
    password = None
    if has_pw and len(rest) >= 0x20:
        password = rest[:0x20].split(b"\x00", 1)[0] or None
        rest = rest[0x20:]
    a = rest[0] if len(rest) > 0 else 0
    b = rest[1] if len(rest) > 1 else 0
    return name, password, a, b


def parse_room_settings(payload):
    """0x450b: u8 flags; char[32] pw; u8 A; u8 B.
    flags: bit0 B valid, bit1 A valid, bit2 new password, bit3 password removed."""
    flags = payload[0] if payload else 0
    pw = payload[1:0x21].split(b"\x00", 1)[0]
    a = payload[0x21] if len(payload) > 0x21 else 0
    b = payload[0x22] if len(payload) > 0x22 else 0
    return flags, pw, a, b


# -- user records (built from a character + where the user is) ----------------

def _char_fields(char):
    s = (list(char.stats) + [0] * 6)[:6] if char else [0] * 6
    f = (list(char.flags) + [0] * 3)[:3] if char else [0, 0, 1]
    # The client's clamps are signed: 0x80..0xff passes and draws a garbage sprite.
    f[RANK] = min(max(f[RANK], 0), 17)
    f[PREF] = min(max(f[PREF], 0), 46)
    return s, f


def user_record(uid, char, room_id=None):
    """0x4404 record (71 B). status/location say lobby vs which room; the client takes
    its CURRENT ROOM from its own record's location (the -9002 lookup)."""
    s, f = _char_fields(char)
    status = STATUS_ROOM if room_id is not None else STATUS_LOBBY
    loc = room_id if room_id is not None else NO_ROOM
    w = wire.Writer().fixed(name_bytes(char.name if char else "PLAYER"), 0x20).u32(uid)
    for v in (s[1], s[2], s[3], s[0], status, s[5], s[4], loc):
        w.u32(v)
    for v in f:
        w.u8(v)
    return w.bytes()


def user_add_record(uid, char, room_id=None):
    """0x4400 push (63 B): adds one user (no s1, no location)."""
    s, f = _char_fields(char)
    status = STATUS_ROOM if room_id is not None else STATUS_LOBBY
    w = wire.Writer().fixed(name_bytes(char.name if char else "PLAYER"), 0x20).u32(uid)
    for v in (s[1], s[2], s[3], s[4], status, s[5]):
        w.u32(v)
    for v in f:
        w.u8(v)
    return w.bytes()


def stats_record(uid, char):
    """0x450f push (31 B): one user's stats."""
    s, f = _char_fields(char)
    w = wire.Writer().u32(uid)
    for v in s:
        w.u32(v)
    for v in f:
        w.u8(v)
    return w.bytes()


# In-memory by default so STATE is never None (tests, imports). The gate replaces
# it with a persistent store via init() from POPN_STATE_DB.
STATE = ServerState(None)


def init(path):
    global STATE
    STATE = ServerState(path)
    return STATE
